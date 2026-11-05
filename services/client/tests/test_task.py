import torch

from fl_client.task import evaluate_local, load_org_data, read_feature_csv, train_local
from fl_common import FEATURE_NAMES, NUM_FEATURES, HoneypotMLP, get_weights


def test_load_synthetic_split():
    data = load_org_data(0, num_orgs=3)
    assert data.X_train.shape[1] == NUM_FEATURES
    assert len(data.y_train) + len(data.y_test) == 2000
    assert len(data.y_test) == 400


def test_training_improves_accuracy():
    torch.manual_seed(0)
    data = load_org_data(1, num_orgs=3)
    model = HoneypotMLP()
    _, before = evaluate_local(model, data.X_test, data.y_test)
    train_local(model, data.X_train, data.y_train, epochs=5, lr=0.01, batch_size=64)
    _, after = evaluate_local(model, data.X_test, data.y_test)
    assert after > max(before, 0.85)


def test_proximal_term_keeps_weights_closer():
    data = load_org_data(0, num_orgs=3)
    torch.manual_seed(0)
    start = HoneypotMLP()
    plain, prox = HoneypotMLP(), HoneypotMLP()
    plain.load_state_dict(start.state_dict())
    prox.load_state_dict(start.state_dict())
    train_local(plain, data.X_train, data.y_train, epochs=3, lr=0.01, batch_size=64)
    train_local(prox, data.X_train, data.y_train, epochs=3, lr=0.01, batch_size=64, proximal_mu=10.0)

    def dist(m):
        return sum(((a - b) ** 2).sum() for a, b in zip(get_weights(m), get_weights(start)))

    assert dist(prox) < dist(plain)


def test_csv_skips_hostile_rows(tmp_path):
    path = tmp_path / "features.csv"
    header = ",".join(FEATURE_NAMES) + ",label"
    good = ",".join(["1"] * NUM_FEATURES) + ",2"
    bad_nan = ",".join(["nan"] * NUM_FEATURES) + ",0"
    bad_text = ",".join(["rm -rf /"] * NUM_FEATURES) + ",0"
    path.write_text("\n".join([header, good, bad_nan, bad_text, good]) + "\n")
    X, y = read_feature_csv(path)
    assert X.shape == (2, NUM_FEATURES)
    assert y.tolist() == [2, 2]


def test_csv_from_log_processor(tmp_path):
    """Same layout as tools/cowrie_dataset output: extra columns, text labels."""
    path = tmp_path / "orgA.csv"
    cols = ["honeypot", "session_id", "src_ip", "start", "label", *FEATURE_NAMES]

    def line(label):
        meta = ["hp", "hp:abc", "1.2.3.4", "2024-10-25T00:00:00Z", label]
        return ",".join(meta + ["1"] * NUM_FEATURES)

    rows = [line("brute_force"), line("persistence"), line("scan"), line("malware_download"), line("ddos")]
    path.write_text("\n".join([",".join(cols), *rows]) + "\n")
    X, y = read_feature_csv(path)
    assert X.shape == (3, NUM_FEATURES)  # scan and the unknown label are skipped
    assert y.tolist() == [0, 2, 3]
