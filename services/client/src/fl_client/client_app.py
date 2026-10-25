"""Flower ClientApp run by each organisation.

Trains HoneypotMLP on the org's local sessions and replies with:
    arrays   the updated weights
    metrics  train loss and num-examples (used by the strategy for weighting)
    meta     UpdateMetadata, which the coordinator uses to reject stale,
             duplicate and incompatible updates

Which org this node is comes from, in order:
    env FL_ORG_ID / FL_ORG_INDEX / FL_DATA_PATH   (ECS Fargate in AWS)
    node config org-id / org-index / data-path    (supernode --node-config)
"""

from __future__ import annotations

import os
import random

from flwr.app import ArrayRecord, ConfigRecord, Context, Message, MetricRecord, RecordDict
from flwr.clientapp import ClientApp

from fl_client.task import evaluate_local, load_org_data, train_local
from fl_common import HoneypotMLP, UpdateMetadata, get_weights, set_weights

app = ClientApp()


def _setting(context: Context, env: str, node_key: str, default=None):
    value = os.environ.get(env)
    if value not in (None, ""):
        return value
    return context.node_config.get(node_key, default)


def _org(context: Context) -> tuple[str, int, int, str | None]:
    org_index = int(_setting(context, "FL_ORG_INDEX", "org-index", context.node_id % 1000))
    org_id = str(_setting(context, "FL_ORG_ID", "org-id", f"org-{org_index}"))
    num_orgs = int(context.run_config.get("num-orgs", 3))
    data_path = _setting(context, "FL_DATA_PATH", "data-path")
    return org_id, org_index, num_orgs, data_path


def _model_from(msg: Message) -> HoneypotMLP:
    model = HoneypotMLP()
    set_weights(model, msg.content["arrays"].to_numpy_ndarrays())
    return model


@app.train()
def train(msg: Message, context: Context) -> Message:
    cfg = context.run_config
    org_id, org_index, num_orgs, data_path = _org(context)

    # Fault injection for experiments (0 in normal runs)
    if random.random() < float(cfg.get("fail-prob", 0.0)):
        raise RuntimeError(f"Simulated client failure on {org_id}")

    data = load_org_data(org_index, num_orgs, data_path, seed=int(cfg.get("seed", 0)))
    model = _model_from(msg)
    server_cfg = msg.content["config"]
    round_number = int(server_cfg["server-round"])

    loss = train_local(
        model,
        data.X_train,
        data.y_train,
        epochs=int(cfg.get("local-epochs", 3)),
        lr=float(cfg.get("lr", 0.01)),
        batch_size=int(cfg.get("batch-size", 64)),
        proximal_mu=float(server_cfg["proximal-mu"]) if "proximal-mu" in server_cfg else 0.0,
        seed=round_number,
    )

    meta = UpdateMetadata(
        client_id=org_id,
        round_number=round_number,
        model_version=int(server_cfg["model-version"]) if "model-version" in server_cfg else 0,
        num_examples=len(data.y_train),
    )
    print(f"[{org_id}] round {round_number}: trained on {meta.num_examples} sessions, loss={loss:.4f}")

    content = RecordDict(
        {
            "arrays": ArrayRecord(get_weights(model)),
            "metrics": MetricRecord({"train_loss": loss, "num-examples": meta.num_examples}),
            "meta": ConfigRecord(meta.to_config()),
        }
    )
    return Message(content=content, reply_to=msg)


@app.evaluate()
def evaluate(msg: Message, context: Context) -> Message:
    org_id, org_index, num_orgs, data_path = _org(context)
    data = load_org_data(org_index, num_orgs, data_path, seed=int(context.run_config.get("seed", 0)))
    loss, acc = evaluate_local(_model_from(msg), data.X_test, data.y_test)
    print(f"[{org_id}] eval: loss={loss:.4f} acc={acc:.3f}")
    metrics = MetricRecord({"eval_loss": loss, "eval_acc": acc, "num-examples": len(data.y_test)})
    return Message(content=RecordDict({"metrics": metrics}), reply_to=msg)
