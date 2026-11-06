"""Run the whole pipeline, or part of it, from raw Cowrie logs to a test report.

Stages, in order. Each one reads files the previous one wrote, so any stage
can be rerun on its own:

    label     raw Kaggle Cowrie logs  -> data/processed/kaggle/sessions.csv
    split     sessions.csv            -> data/processed/kaggle/split/train/orgX.csv
                                         data/processed/kaggle/split/replay/orgX/cowrie.json
    train     train/orgX.csv          -> models/<run-name>/ (checkpoints + model.npz)
              (local Docker stack: 1 coordinator + 3 org clients, real data)
    replay    replay Cowrie logs      -> data/processed/replay/sessions.csv
    evaluate  saved model + replay CSV -> models/<run-name>/eval_report.json

The replay stage labels whatever Cowrie logs you point --replay-logs at. By
default that is the held-out events split_orgs.py set aside, so the whole
pipeline runs offline. After the real replay on the honeypots, point it at
the folder of logs Cowrie produced (one sub-folder per org).

label, split and replay are skipped when their output is already newer than
their input (use --force to redo them). train always starts a new run.

Usage (from the repo root, venv active, Docker running):
    python tools/run_pipeline.py                              # everything
    python tools/run_pipeline.py --from train --strategy fedprox
    python tools/run_pipeline.py --only evaluate --run-name 20261106-0930-fedavg
    python tools/run_pipeline.py --from replay --replay-logs data/raw/replay-logs
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SESSIONS = ROOT / "data/processed/kaggle/sessions.csv"
SPLIT = ROOT / "data/processed/kaggle/split"  # fixed: compose.real-data.yaml mounts it
MODELS = ROOT / "models"                       # fixed: compose.yaml mounts it
COMPOSE = [ROOT / "deploy/local/compose.yaml", ROOT / "deploy/local/compose.real-data.yaml"]
FLOWER_APP = ROOT / "deploy/flower-app"
ORGS = ("orgA", "orgB", "orgC")

STAGES = ["label", "split", "train", "replay", "evaluate"]


def run(cmd: list[str], cwd: Path = ROOT) -> None:
    print(f"\n$ {' '.join(map(str, cmd))}", flush=True)
    code = subprocess.call([str(c) for c in cmd], cwd=cwd)
    if code != 0:
        sys.exit(f"Command failed with exit code {code}")


def newest_mtime(path: Path) -> float:
    if path.is_file():
        return path.stat().st_mtime
    times = [p.stat().st_mtime for p in path.rglob("*") if p.is_file()]
    return max(times, default=0.0)


def up_to_date(outputs: list[Path], inputs: list[Path]) -> bool:
    if not all(o.exists() for o in outputs):
        return False
    oldest_out = min(o.stat().st_mtime for o in outputs)
    return oldest_out >= max((newest_mtime(i) for i in inputs), default=0.0)


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"'{tool}' not found on PATH (activate the venv / start Docker Desktop).")
    return path


# ---- stages ------------------------------------------------------------------


def stage_label(a) -> None:
    if not a.force and up_to_date([SESSIONS], [a.logs]):
        print(f"label: {SESSIONS.relative_to(ROOT)} is up to date, skipping")
        return
    run([sys.executable, TOOLS / "cowrie_dataset/cowrie_sessions.py", "label", "--logs", a.logs, "--out", SESSIONS])


def stage_split(a) -> None:
    outputs = [SPLIT / "train" / f"{o}.csv" for o in ORGS] + [SPLIT / "replay" / o / "cowrie.json" for o in ORGS]
    if not a.force and up_to_date(outputs, [SESSIONS]):
        print("split: train/ and replay/ are up to date, skipping")
        return
    run([sys.executable, TOOLS / "cowrie_dataset/split_orgs.py",
         "--sessions", SESSIONS, "--logs", a.logs, "--out", SPLIT])


def stage_train(a) -> None:
    for o in ORGS:
        if not (SPLIT / "train" / f"{o}.csv").is_file():
            sys.exit(f"Missing {SPLIT / 'train' / f'{o}.csv'}: run the split stage first.")
    flwr = need("flwr")
    MODELS.mkdir(exist_ok=True)
    if not a.no_docker:
        docker = need("docker")
        run([docker, "compose", *sum((["-f", c] for c in COMPOSE), []), "up", "--build", "-d"])
        print(f"Waiting {a.startup_wait}s for the SuperLink and SuperNodes to start...")
        time.sleep(a.startup_wait)

    config = {
        "run-name": f"'{a.run_name}'",
        "strategy": f"'{a.strategy}'",
        "num-server-rounds": a.rounds,
        "fail-prob": a.fail_prob,
    }
    run_config = " ".join(f"{k}={v}" for k, v in config.items())
    if a.train_config:
        run_config += " " + a.train_config
    try:
        run([flwr, "run", ".", a.federation, "--stream", "--run-config", run_config], cwd=FLOWER_APP)
    finally:
        if a.down and not a.no_docker:
            run([need("docker"), "compose", *sum((["-f", c] for c in COMPOSE), []), "down"])

    final = MODELS / a.run_name / "model.npz"
    if not final.is_file():
        sys.exit(f"Training finished but {final} was not written. Check the coordinator logs "
                 f"(docker compose -f deploy/local/compose.yaml logs superlink).")
    print(f"train: global model saved to {final.relative_to(ROOT)}")


def stage_replay(a) -> None:
    if not a.force and up_to_date([a.replay_out], [a.replay_logs]):
        print(f"replay: {a.replay_out.relative_to(ROOT)} is up to date, skipping")
        return
    run([sys.executable, TOOLS / "cowrie_dataset/cowrie_sessions.py", "label",
         "--logs", a.replay_logs, "--out", a.replay_out])


def stage_evaluate(a) -> None:
    run_dir = MODELS / a.run_name
    if not (run_dir / "model.npz").is_file():
        sys.exit(f"No model at {run_dir / 'model.npz'}: run the train stage or pass --run-name.")
    if not a.replay_out.is_file():
        sys.exit(f"No replay CSV at {a.replay_out}: run the replay stage first.")
    run([sys.executable, TOOLS / "evaluate_model.py", "--model", run_dir, "--data", a.replay_out])


def latest_run() -> str | None:
    runs = [p for p in MODELS.glob("*/model.npz")] if MODELS.is_dir() else []
    return max(runs, key=lambda p: p.stat().st_mtime).parent.name if runs else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", choices=STAGES, default=STAGES[0], help="first stage to run")
    ap.add_argument("--to", dest="end", choices=STAGES, default=STAGES[-1], help="last stage to run")
    ap.add_argument("--only", choices=STAGES, help="run just this stage")
    ap.add_argument("--force", action="store_true", help="redo label/split/replay even if up to date")
    ap.add_argument("--logs", type=Path, default=ROOT / "data/raw/kaggle-cowrie/logs", help="raw Kaggle logs")
    ap.add_argument("--replay-logs", type=Path, default=SPLIT / "replay",
                    help="Cowrie logs to test on, one sub-folder per org (default: held-out split events)")
    ap.add_argument("--replay-out", type=Path, default=ROOT / "data/processed/replay/sessions.csv")
    # training
    ap.add_argument("--run-name", help="model folder under models/ (default: new for train, latest for evaluate)")
    ap.add_argument("--strategy", default="fedavg", choices=["fedavg", "fedmedian", "fedprox", "adaptive"])
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--fail-prob", type=float, default=0.0, help="simulated client crash probability")
    ap.add_argument("--train-config", default="", help='extra flwr --run-config, e.g. "lr=0.005 local-epochs=5"')
    ap.add_argument("--federation", default="local-deployment")
    ap.add_argument("--no-docker", action="store_true", help="SuperLink/SuperNodes already running, skip compose")
    ap.add_argument("--down", action="store_true", help="stop the Docker stack after training")
    ap.add_argument("--startup-wait", type=int, default=10, help="seconds to wait after docker compose up")
    a = ap.parse_args()

    for name in ("logs", "replay_logs", "replay_out"):
        p = getattr(a, name)
        setattr(a, name, p if p.is_absolute() else (Path.cwd() / p).resolve())

    stages = [a.only] if a.only else STAGES[STAGES.index(a.start): STAGES.index(a.end) + 1]
    if not stages:
        sys.exit("--from comes after --to")
    if a.run_name is None:
        if "train" in stages:
            a.run_name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{a.strategy}"
        elif "evaluate" in stages:
            a.run_name = latest_run()
            if a.run_name is None:
                sys.exit("No trained model in models/. Train first or pass --run-name.")
            print(f"Using the latest run: {a.run_name}")

    print(f"Stages: {', '.join(stages)}" + (f" | run: {a.run_name}" if a.run_name else ""))
    for s in stages:
        print(f"\n########## {s.upper()} ##########", flush=True)
        globals()[f"stage_{s}"](a)
    print("\nPipeline finished.")


if __name__ == "__main__":
    main()
