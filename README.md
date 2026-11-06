# CSC3104-honeypot-fl

Fault Tolerant Federated Learning for Collaborative Honeypot Intrusion Detection.

## Repository structure

```
.
├── .github/workflows/      CI pipelines
├── deploy/
│   ├── ecs/                Client deployment for customer AWS accounts
│   └── k8s/
│       ├── base/           Shared Kubernetes manifests
│       └── overlays/
│           ├── local/      Settings for a local cluster
│           └── aws/        Settings for the AWS clusters
├── docs/adr/               Architecture decision records
├── experiments/            Evaluation scenarios and results
├── infra/                  AWS infrastructure as code
├── libs/fl-common/         Code shared between services
└── services/
    ├── client/             Flower client (one per organisation)
    ├── coordinator/        Flower server and aggregation strategy
    ├── cowrie/             Cowrie SSH honeypot configuration
    ├── log-processor/      Turns Cowrie logs into training features
    └── model-api/          Serves global model versions
```

## What goes in each folder

### `services/`
Each subfolder is one independently built and deployed component. Every service should contain its own `Dockerfile`, dependency file (e.g. `pyproject.toml`), source code and `tests/`.

| Folder | Contents |
|---|---|
| `coordinator/` | The Flower server: round management, FedAvg aggregation, quorum-based round completion, stale/duplicate update rejection and client resynchronisation. |
| `client/` | The Flower client run by each organisation: loads local features, trains the local model and sends only model parameters to the coordinator. |
| `cowrie/` | Configuration for the Cowrie SSH honeypot (config files, Dockerfile if customised). Cowrie is treated as untrusted and must stay isolated from training services. |
| `log-processor/` | Reads Cowrie JSON logs and converts them into feature data for the client. Logs are attacker-controlled, so this must validate input and never execute anything from it. |
| `model-api/` | A small API that serves the latest and past global model versions. |

### `libs/fl-common/`
Code used by more than one service, so it is defined once. For example: the model definition, the feature schema, and the update metadata format (round number, model version, update ID).

### `deploy/`
Everything needed to run the services, but no application code.

- `k8s/base/` holds manifests shared by every environment: Deployments, Services, NetworkPolicies, health checks, and CPU/memory requests and limits for the coordinator and model API.
- `k8s/overlays/local/` holds Kustomize patches for a local cluster (e.g. kind or k3d) used during development.
- `k8s/overlays/aws/` holds Kustomize patches for the AWS deployment (image registry, load balancer, storage).
- `ecs/` holds the templates (e.g. CloudFormation and ECS task definitions) that let each simulated customer deploy the client in their own AWS account on ECS Fargate.

### `infra/`
Infrastructure as code for the provider side on AWS: networking, EC2 instances for the Kubernetes cluster, S3 buckets, ECR repositories and IAM roles. Nothing should be created by hand in the AWS console without being captured here.

### `experiments/`
Everything needed to reproduce the evaluation: scenario configs (e.g. baseline FedAvg vs our coordinator, slow client, client failure and recovery), scripts to run them and collect results, and the resulting data and plots used in the report.

### `docs/adr/`
Architecture decision records: short notes on each significant design choice (what was decided, why, and what alternatives were rejected). Name them `0001-short-title.md`, `0002-...` and so on. These are useful when writing the final report.

### `.github/workflows/`
GitHub Actions pipelines: linting, tests, building Docker images and pushing them to a registry.

## ML pipeline

From raw Cowrie logs to a tested global model. Each stage reads the files the
previous one wrote, so any stage can be rerun on its own.

| # | Stage | Command | Output |
|---|---|---|---|
| 1 | label | `python tools/cowrie_dataset/cowrie_sessions.py label` | `data/processed/kaggle/sessions.csv` |
| 2 | split | `python tools/cowrie_dataset/split_orgs.py` | `data/processed/kaggle/split/train/orgX.csv`, `split/replay/orgX/cowrie.json` |
| 3 | train | `flwr run . local-deployment --stream` (in `deploy/flower-app`, local stack running) | `models/<run-name>/` |
| 4 | replay | replay the held-out attacks, then `cowrie_sessions.py label --logs <replay logs> --out data/processed/replay/sessions.csv` | `data/processed/replay/sessions.csv` |
| 5 | evaluate | `python tools/evaluate_model.py --model models/<run-name> --data data/processed/replay/sessions.csv` | `models/<run-name>/eval_report.json` |

`python tools/run_pipeline.py` runs all five in order (Docker running, venv active).
It skips label, split and replay when their output is already up to date, and
accepts `--from`, `--to`, `--only`, `--strategy`, `--rounds` and `--run-name`.
Without `--replay-logs` it tests on the held-out events from the split, so it
works offline; after the real replay, point `--replay-logs` at the Cowrie logs
it produced (one sub-folder per org).

Only the global model is saved: a checkpoint after every successful round
(`round_NNN.npz`) and the final model (`model.npz`). Each `.npz` has a `.json`
next to it with the schema and architecture versions and a SHA-256, which
`fl_common.load_model` checks before using it. To continue a run that stopped,
pass `--run-config "resume-from='/app/models/<run-name>'"` to `flwr run`.

## Rules for the repo

Cowrie Kubernetes setup: [deployment and recovery guide](docs/cowrie-kubernetes.md).
Local development: [three kind clusters, storage, autoscaling and S3 uploads](docs/cowrie-local.md).
ML team: [S3 access, current data paths and duplicate-handling contract](docs/cowrie-ml-handoff.md).
Cowrie configuration targets three separate EKS clusters (`orga-honeypot-cluster`,
`orgb-honeypot-cluster`, `orgc-honeypot-cluster`), each using `default`.
The AWS Cowrie overlays are independent templates; render local settings and use
the context-checked command wrapper in the guide for every cluster operation.

- Never commit secrets, `.env` files, AWS keys, Cowrie logs, datasets or model checkpoints.
- Do not push directly to `main`; open a pull request from a feature branch.
- Remove a folder's `.gitkeep` once real files are added to it.
