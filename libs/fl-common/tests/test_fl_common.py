import math

import numpy as np
import pytest
import torch

from fl_common import (
    FEATURE_NAMES,
    NUM_CLASSES,
    NUM_FEATURES,
    AttackClass,
    HoneypotMLP,
    SchemaError,
    UpdateMetadata,
    Verdict,
    check_update,
    get_weights,
    label_session,
    parse_label,
    set_weights,
    transform,
    validate_row,
)
from fl_common.synthetic import make_org_dataset


def _row(**overrides):
    row = dict.fromkeys(FEATURE_NAMES, 0.0)
    row.update(overrides)
    return row


# ---- schema ----------------------------------------------------------------

def test_schema_import_does_not_need_torch():
    """The log processor Lambda imports the schema without torch or numpy."""
    import subprocess
    import sys

    code = (
        "import sys; from fl_common.schema import validate_row; "
        "import fl_common; "
        "assert 'torch' not in sys.modules and 'numpy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_validate_row_orders_and_clips():
    values = validate_row(_row(duration_s=1e9, n_commands=3, cmd_exec=5))
    assert len(values) == NUM_FEATURES
    assert values[FEATURE_NAMES.index("duration_s")] == 86_400.0
    assert values[FEATURE_NAMES.index("n_commands")] == 3.0
    assert values[FEATURE_NAMES.index("cmd_exec")] == 1.0  # flags clip to 1


@pytest.mark.parametrize(
    "bad",
    [
        {"duration_s": "rm -rf /"},
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"duration_s": -1},
        {"duration_s": True},
        {"evil_field": 1},
    ],
)
def test_validate_row_rejects_bad_input(bad):
    with pytest.raises(SchemaError):
        validate_row(_row(**bad))


def test_validate_row_rejects_missing():
    row = _row()
    del row["duration_s"]
    with pytest.raises(SchemaError):
        validate_row(row)


def test_label_session_rules():
    assert label_session(_row()) is None  # scan
    assert label_session(_row(n_tcpip_requests=2, n_login_success=1)) is None  # tunnel
    assert label_session(_row(n_login_failed=1)) is AttackClass.BRUTE_FORCE
    assert label_session(_row(n_login_success=1)) is AttackClass.BRUTE_FORCE
    assert label_session(_row(n_login_success=1, n_commands=2)) is AttackClass.RECON
    assert label_session(_row(n_commands=4, cmd_persistence=1)) is AttackClass.PERSISTENCE
    # most serious rule wins
    assert label_session(_row(n_commands=4, cmd_persistence=1, n_file_uploads=1)) is AttackClass.MALWARE_DOWNLOAD
    assert label_session(_row(n_commands=1, cmd_download=1)) is AttackClass.MALWARE_DOWNLOAD


def test_parse_label():
    assert parse_label("persistence") is AttackClass.PERSISTENCE
    assert parse_label("Malware_Download") is AttackClass.MALWARE_DOWNLOAD
    assert parse_label("0") is AttackClass.BRUTE_FORCE
    assert parse_label("scan") is None
    assert parse_label("tunnel") is None
    with pytest.raises(SchemaError):
        parse_label("ddos")
    with pytest.raises(SchemaError):
        parse_label("9")


def test_transform_is_log1p():
    out = transform([0.0] * (NUM_FEATURES - 1) + [math.e - 1])
    assert out[-1] == pytest.approx(1.0)


# ---- model -----------------------------------------------------------------

def test_model_output_shape():
    model = HoneypotMLP()
    assert model(torch.zeros(8, NUM_FEATURES)).shape == (8, NUM_CLASSES)


def test_weights_round_trip():
    a, b = HoneypotMLP(), HoneypotMLP()
    set_weights(b, get_weights(a))
    for wa, wb in zip(get_weights(a), get_weights(b)):
        np.testing.assert_array_equal(wa, wb)


def test_set_weights_rejects_wrong_shape():
    weights = get_weights(HoneypotMLP())
    weights[0] = np.zeros((1, 1), dtype=np.float32)
    with pytest.raises(ValueError):
        set_weights(HoneypotMLP(), weights)


# ---- metadata --------------------------------------------------------------

def _meta(**kw):
    base = dict(client_id="org-a", round_number=3, model_version=2, num_examples=100)
    base.update(kw)
    return UpdateMetadata(**base)


def test_metadata_config_round_trip():
    meta = _meta()
    assert UpdateMetadata.from_config(meta.to_config()) == meta


def test_metadata_rejects_malformed():
    cfg = _meta().to_config()
    del cfg["meta.round_number"]
    with pytest.raises(ValueError):
        UpdateMetadata.from_config(cfg)


@pytest.mark.parametrize(
    "kwargs, seen, expected",
    [
        ({}, set(), Verdict.ACCEPT),
        ({"update_id": "x"}, {"x"}, Verdict.DUPLICATE),
        ({"round_number": 2}, set(), Verdict.STALE_ROUND),
        ({"model_version": 1}, set(), Verdict.STALE_MODEL),
        ({"num_examples": 0}, set(), Verdict.EMPTY),
        ({"schema_version": 99}, set(), Verdict.INCOMPATIBLE),
    ],
)
def test_check_update(kwargs, seen, expected):
    verdict = check_update(
        _meta(**kwargs), current_round=3, current_model_version=2, seen_update_ids=seen
    )
    assert verdict is expected


# ---- synthetic data --------------------------------------------------------

def test_synthetic_is_deterministic_and_non_iid():
    X0, y0 = make_org_dataset(0, num_orgs=4, num_sessions=500)
    X0b, y0b = make_org_dataset(0, num_orgs=4, num_sessions=500)
    np.testing.assert_array_equal(X0, X0b)
    np.testing.assert_array_equal(y0, y0b)

    _, y1 = make_org_dataset(1, num_orgs=4, num_sessions=500)
    d0 = np.bincount(y0, minlength=NUM_CLASSES) / len(y0)
    d1 = np.bincount(y1, minlength=NUM_CLASSES) / len(y1)
    assert np.abs(d0 - d1).sum() > 0.2  # orgs see different attack mixes


def test_model_learns_synthetic_data():
    torch.manual_seed(0)
    X, y = make_org_dataset(0, num_orgs=1, num_sessions=1500)
    Xt = torch.tensor([transform(r) for r in X.tolist()], dtype=torch.float32)
    yt = torch.tensor(y)
    model = HoneypotMLP()
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    for _ in range(200):
        opt.zero_grad()
        loss = torch.nn.functional.cross_entropy(model(Xt), yt)
        loss.backward()
        opt.step()
    acc = (model(Xt).argmax(1) == yt).float().mean().item()
    assert acc > 0.9


# ---- log processor agrees with the schema ------------------------------------

def _log_processor():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "tools" / "cowrie_dataset" / "cowrie_sessions.py"
    if not path.is_file():
        pytest.skip("tools/cowrie_dataset not present")
    spec = importlib.util.spec_from_file_location("cowrie_sessions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_log_processor_features_match_schema():
    assert tuple(_log_processor().FEATURES) == FEATURE_NAMES


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"n_login_failed": 1},
        {"n_login_success": 1, "n_tcpip_requests": 3},
        {"n_login_success": 1, "n_commands": 2},
        {"n_commands": 2, "cmd_persistence": 1},
        {"n_commands": 2, "cmd_persistence": 1, "n_url_downloads": 1},
        {"n_commands": 1, "cmd_download": 1},
    ],
)
def test_log_processor_labels_match_schema(state):
    lp = _log_processor()
    s = lp._new_session("hp", "sid")
    s.update(state)
    text = lp.label(s)
    assert parse_label(text) == label_session(_row(**state))
