"""Flower ServerApp: the coordinator.

Run settings come from [tool.flwr.app.config] in deploy/flower-app/pyproject.toml
(or `flwr run ... --run-config "key=value"`).

The global model is saved after every successful round and at the end of the
run (see checkpoints.py). Set save-model=false to turn that off.

Where a run starts from:
    (default)                  a fresh, randomly initialised model
    init-from='latest'         the model latest.json points to: the daily cycle
                               (first ever run has no latest yet, so it starts fresh)
    init-from=<folder|.npz>    a specific earlier model
    resume-from=<run folder>   crash recovery: continue THAT run from its last
                               checkpoint, in the same folder

Bootstrap: a run that starts from a fresh model (no init-from, or init-from='latest'
before any model was promoted) tells the clients to train on their base data only
(the Kaggle split) and ignore nightly batches, so the first model is always
built from the curated dataset. Set bootstrap-with-batches=true to include them.
A run that starts from an existing model always uses the batches.

At the end of a run (see publish.py):
    eval-data=<csv[,csv]>      score the final model on this fixed test set and
                               write eval_report.json
    promote=true               move latest.json to this run if the score is not
                               worse than the current latest (promote-tolerance)
    publish-s3-uri=s3://...    also copy the promoted model and latest.json to S3
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from fl_common import HoneypotMLP, get_weights, load_model, load_weights
from fl_coordinator.checkpoints import RunStore, default_model_dir
from fl_coordinator.publish import (
    SCORE_KEY,
    read_latest,
    resolve_init,
    should_promote,
    upload_to_s3,
    write_latest,
)
from fl_coordinator.runner import FaultTolerantRunner
from fl_coordinator.strategies import make_strategy

app = ServerApp()


def _paths(value: str) -> list[Path]:
    return [Path(p.strip()) for p in value.split(",") if p.strip()]


def finish_run(store: RunStore, model_dir: Path, cfg) -> None:
    """Evaluate the final model, decide on promotion, publish. Never raises:
    a failure here must not lose the model that was just trained."""
    final = store.run_dir / "model.npz"
    if not final.is_file():
        return
    score = None
    eval_paths = _paths(str(cfg.get("eval-data", "")))
    if eval_paths:
        try:
            from fl_common.evaluation import evaluate

            model, info = load_model(final)
            report = {"model": str(final), "model_info": info, **evaluate(model, eval_paths)}
            (store.run_dir / "eval_report.json").write_text(json.dumps(report, indent=2, default=str))
            score = report["overall"][SCORE_KEY]
            store.add_to_history(evaluation={
                "data": [str(p) for p in eval_paths],
                "accuracy": report["overall"]["accuracy"],
                SCORE_KEY: score,
                "per_org_" + SCORE_KEY: {o: m[SCORE_KEY] for o, m in report["per_org"].items()},
            })
            print(f"Fixed test set: accuracy {report['overall']['accuracy']:.4f}, {SCORE_KEY} {score:.4f}")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: evaluation on {eval_paths} failed ({exc}); not promoting this run.")
            store.add_to_history(evaluation={"error": str(exc)}, promotion={"promoted": False, "reason": "evaluation failed"})
            return

    if not bool(cfg.get("promote", False)):
        store.add_to_history(promotion={"promoted": False, "reason": "promote=false"})
        return
    current = read_latest(model_dir)
    ok, reason = should_promote(score, current, float(cfg.get("promote-tolerance", 0.0)))
    print(f"Promotion: {'yes' if ok else 'no'} ({reason})")
    promotion = {"promoted": ok, "reason": reason, "previous": current["run"] if current else None}
    if ok:
        info = load_weights(final)[1]
        latest = write_latest(model_dir, {
            "run": store.run_dir.name, "model_version": info.get("model_version"),
            "score": score, "reason": reason,
        })
        uri = str(cfg.get("publish-s3-uri", "")).strip()
        if uri:
            files = [store.run_dir / n for n in ("model.npz", "model.json", "history.json", "eval_report.json")]
            store.add_to_history(promotion=promotion)  # so the uploaded history.json includes it
            try:
                promotion["s3"] = upload_to_s3(uri, [f for f in files if f.is_file()], latest)
                print(f"Published to {uri}")
            except Exception as exc:  # noqa: BLE001
                promotion["s3_error"] = str(exc)
                print(f"WARNING: S3 publish to {uri} failed ({exc}); the local latest.json is still updated.")
    store.add_to_history(promotion=promotion)


@app.main()
def main(grid: Grid, context: Context) -> None:
    cfg = context.run_config
    quorum = int(cfg.get("quorum", 2))
    total_rounds = int(cfg.get("num-server-rounds", 5))
    strategy_name = str(cfg.get("strategy", "fedavg"))

    torch.manual_seed(int(cfg.get("seed", 0)))
    initial = ArrayRecord(get_weights(HoneypotMLP()))

    strategy = make_strategy(
        strategy_name,
        fraction_train=1.0,
        fraction_evaluate=1.0,
        min_train_nodes=quorum,
        min_evaluate_nodes=quorum,
        min_available_nodes=quorum,
    )
    runner = FaultTolerantRunner(
        strategy,
        quorum=quorum,
        max_retries=int(cfg.get("max-retries", 2)),
        round_timeout=float(cfg.get("round-timeout", 120.0)),
        abort_on_failed_round=bool(cfg.get("abort-on-failed-round", False)),
    )

    # Where the global model goes, and where this run starts from.
    store: RunStore | None = None
    start_round = 1
    model_dir = Path(str(cfg.get("model-dir", "")).strip() or default_model_dir())
    if bool(cfg.get("save-model", True)):
        settings = {k: v for k, v in cfg.items()}
        resume_from = str(cfg.get("resume-from", "")).strip()
        if resume_from:
            store, weights, last_round, model_version = RunStore.resume(Path(resume_from), settings)
            initial = ArrayRecord(weights)
            runner.model_version = model_version
            start_round = last_round + 1
        else:
            store = RunStore.new(model_dir, str(cfg.get("run-name", "")).strip(), settings)
            print(f"Saving global model checkpoints to {store.run_dir.resolve()}")

    fresh = start_round == 1   # until we know it starts from an existing model
    if start_round > 1 and store is not None:
        # resumed run: keep whatever it was (a resumed bootstrap stays a bootstrap)
        fresh = bool(store.extra.get("bootstrap", False))
    init_spec = str(cfg.get("init-from", "")).strip()
    if init_spec and start_round == 1:
        source = resolve_init(init_spec, model_dir)
        if source is None:
            print(f"init-from={init_spec!r}: no model to start from yet, starting from a fresh model.")
            init = {"spec": init_spec, "model": None}
        else:
            weights, info = load_weights(source)
            initial = ArrayRecord(weights)
            # Keep counting versions, so v(n+1) always means "newer than v(n)".
            runner.model_version = int(info.get("model_version", 0))
            print(f"Starting from {source}.npz (run {info.get('run')}, v{runner.model_version})")
            init = {"spec": init_spec, "model": str(source) + ".npz", "run": info.get("run"),
                    "model_version": runner.model_version}
            fresh = False
        if store is not None:
            store.extra["init_from"] = init

    bootstrap = fresh and not bool(cfg.get("bootstrap-with-batches", False))
    if bootstrap:
        runner.round_config["bootstrap"] = True
        print("Bootstrap run: fresh model, clients train on their base data only (no nightly batches).")
    if store is not None:
        store.extra["bootstrap"] = bootstrap

    remaining = total_rounds - start_round + 1
    if remaining <= 0:
        print(f"Run already has {start_round - 1} of {total_rounds} rounds; nothing to train.")
    result = runner.run(
        grid,
        initial,
        num_rounds=max(remaining, 0),
        start_round=start_round,
        on_round_end=store.save_round if store else None,
    )

    if store is not None:
        failed = [log.round for log in runner.history if not log.succeeded]
        if store.save_final(result.arrays, failed) is not None:
            finish_run(store, model_dir, cfg)
