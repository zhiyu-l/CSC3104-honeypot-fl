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
