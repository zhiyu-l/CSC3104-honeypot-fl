"""Tests for the daily cycle: latest pointer, promotion gate, trigger, data records."""

import json

import pytest
from flwr.app import ArrayRecord, ConfigRecord, MetricRecord, RecordDict

from fl_common import HoneypotMLP, UpdateMetadata, get_weights, save_model
from fl_coordinator.checkpoints import RunStore
from fl_coordinator.publish import read_latest, resolve_init, should_promote, write_latest
from fl_coordinator.runner import RoundLog, data_summary
from fl_coordinator.trigger import active_runs, build_run_config, write_flwr_config


def make_run(model_dir, name, version):
    save_model(model_dir / name / "model", get_weights(HoneypotMLP()), {"run": name, "model_version": version})
    return model_dir / name


# ---- latest pointer / init-from ---------------------------------------------


def test_no_latest_means_fresh_start(tmp_path):
    assert resolve_init("latest", tmp_path) is None
    assert resolve_init("", tmp_path) is None


def test_latest_resolves_to_run_model(tmp_path):
    make_run(tmp_path, "daily-1", 5)
    write_latest(tmp_path, {"run": "daily-1", "model_version": 5, "score": 0.9})
    assert resolve_init("latest", tmp_path) == tmp_path / "daily-1" / "model"
    assert read_latest(tmp_path)["run"] == "daily-1"


def test_init_from_folder_and_checkpoint(tmp_path):
    run = make_run(tmp_path, "r", 1)
    save_model(run / "round_002", get_weights(HoneypotMLP()), {"model_version": 2})
    assert resolve_init(str(run), tmp_path) == run / "model"
    assert resolve_init(str(run / "round_002.npz"), tmp_path) == run / "round_002"
    with pytest.raises(FileNotFoundError):
        resolve_init(str(tmp_path / "nope"), tmp_path)


# ---- promotion gate -----------------------------------------------------------


@pytest.mark.parametrize("new,old,tol,expected", [
    (0.90, None, 0.0, True),      # first model ever
    (0.95, 0.90, 0.0, True),      # better
    (0.90, 0.90, 0.0, True),      # equal
    (0.89, 0.90, 0.0, False),     # worse
    (0.89, 0.90, 0.02, True),     # worse but within tolerance
    (None, 0.90, 0.0, True),      # no eval-data: cannot check
])
def test_should_promote(new, old, tol, expected):
    current = None if old is None else {"run": "prev", "score": old}
    ok, reason = should_promote(new, current, tol)
    assert ok is expected and reason


def test_rollback_cli(tmp_path, monkeypatch, capsys):
    from fl_coordinator import publish

    make_run(tmp_path, "good", 3)
    (tmp_path / "good" / "eval_report.json").write_text(json.dumps({"overall": {"macro_f1": 0.97}}))
    make_run(tmp_path, "bad", 4)
    write_latest(tmp_path, {"run": "bad", "model_version": 4, "score": 0.5})
    monkeypatch.setattr("sys.argv", ["publish", "--model-dir", str(tmp_path), "--set", "good"])
    publish.main()
    latest = read_latest(tmp_path)
    assert latest["run"] == "good" and latest["score"] == 0.97 and latest["model_version"] == 3


# ---- trigger --------------------------------------------------------------------


def test_active_runs_only_unfinished():
    out = json.dumps({"success": True, "runs": [
        {"run-id": "1", "status": "finished:completed"},
        {"run-id": "2", "status": "finished:failed"},
        {"run-id": "3", "status": "running"},
        {"run-id": "4", "status": "pending"},
    ]})
    assert [r["run-id"] for r in active_runs(out)] == ["3", "4"]
    assert active_runs(json.dumps({"success": True, "runs": []})) == []
    with pytest.raises(RuntimeError):
        active_runs(json.dumps({"success": False}))


def test_run_config_continues_from_latest():
    cfg = build_run_config("daily-x", "num-server-rounds=10")
    assert "init-from='latest'" in cfg and "promote=true" in cfg
    assert "run-name='daily-x'" in cfg and cfg.endswith("num-server-rounds=10")


def test_flwr_config(tmp_path):
    write_flwr_config(tmp_path, "coord:8000", insecure=True, root_certificates=None)
    text = (tmp_path / "config.toml").read_text()
    assert "[superlink.trigger]" in text and 'address = "coord:8000"' in text and "insecure = true" in text


# ---- what each org trained on ends up in history.json ------------------------


class FakeReply:
    def __init__(self, content):
        self.content = content

    def has_error(self):
        return False


def test_data_summary_recorded_per_round(tmp_path):
    meta = UpdateMetadata(client_id="org-b", round_number=1, model_version=0, num_examples=10)
    msg = FakeReply(RecordDict({
        "arrays": ArrayRecord(get_weights(HoneypotMLP())),
        "metrics": MetricRecord({"num-examples": 10}),
        "meta": ConfigRecord(meta.to_config()),
        "data": ConfigRecord({"n-base": 8, "n-batches": 1, "batches": "b1", "source": "fresh"}),
    }))
    client, summary = data_summary(msg)
    assert client == "org-b" and summary["batches"] == "b1"

    store = RunStore.new(tmp_path, "r", {"strategy": "fedavg"})
    store.extra["init_from"] = {"spec": "latest", "run": "prev"}
    log = RoundLog(round=1, succeeded=True, model_version=1, data={client: summary})
    store.save_round(log, ArrayRecord(get_weights(HoneypotMLP())), None)
    store.save_final(ArrayRecord(get_weights(HoneypotMLP())), [])
    store.add_to_history(promotion={"promoted": True})
    h = json.loads((tmp_path / "r" / "history.json").read_text())
    assert h["rounds"][0]["data"]["org-b"]["n-base"] == 8
    assert h["init_from"]["run"] == "prev" and h["promotion"]["promoted"] is True


def test_round_config_sent_with_train_and_evaluate():
    from fl_coordinator.runner import FaultTolerantRunner
    from fl_coordinator.strategies import make_strategy

    seen = []

    class Grid:
        def send_and_receive(self, messages, timeout):
            return []

    strategy = make_strategy("fedavg", min_train_nodes=1, min_available_nodes=1)

    def configure_train(rnd, arrays, config, grid):
        seen.append(dict(config))
        return []

    strategy.configure_train = configure_train
    runner = FaultTolerantRunner(strategy, quorum=1, max_retries=0)
    runner.round_config["bootstrap"] = True
    runner.run(Grid(), ArrayRecord(get_weights(HoneypotMLP())), num_rounds=1)
    assert seen and seen[0]["bootstrap"] is True and "model-version" in seen[0]
