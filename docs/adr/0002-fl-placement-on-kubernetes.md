# 0002: Where the FL client and coordinator run

Status: proposed. Date: 7 November 2026.

## Context

Each organisation has its own EKS cluster with Cowrie in `default`
([ADR 0001](0001-separate-cowrie-clusters.md)). Cowrie pods are attacker-facing,
scaled 1-3 by an HPA and restarted freely. The FL client (Flower SuperNode) must
be exactly one stable node per organisation, and the coordinator (SuperLink +
ServerApp) exactly one for the whole federation.

## Decision

- **FL client:** its own Deployment, `replicas: 1`, `Recreate` strategy, in a
  separate `fl` namespace in each org cluster. Not a sidecar in Cowrie pods: the
  HPA would create 1-3 nodes per org, and attacker-reachable pods would hold the
  model and S3 read access. It only dials out to the coordinator.
- **FL client on AWS:** the `fl` namespace runs on a Fargate profile (private
  subnets), so the client never shares a node with Cowrie. AWS credentials come
  from IRSA (EKS Pod Identity does not support Fargate), scoped to the org's own
  `processed/` prefix. Isolation from Cowrie uses security groups for pods; the
  NetworkPolicy in the base only applies where the CNI enforces it (not Fargate).
- **Training data:** an init container copies the org's processed feature CSV
  from its own S3 bucket to `/data/train.csv` at pod start. Restart the pod to pick
  up new features. Locally, the CSV comes from a ConfigMap instead.
- **Coordinator:** a single container, not on Kubernetes. Locally it is the
  `superlink` service from `deploy/local/compose.yaml`, joined to the `kind`
  network by `compose.kind.yaml`. On AWS it is one ECS Fargate task behind an NLB
  on TCP 8000 (Flower 1.40 serves the Fleet and Control APIs on that one port),
  with checkpoints on EFS at `/models`.
- **Coordinator recovery:** clients reconnect on their own; a stopped run is
  continued with `resume-from` from the last checkpoint.

## Ownership (AWS)

| Who | What |
| --- | --- |
| Mervyn | `fl` namespace and Fargate profile in each org cluster |
| Mervyn + Jun Hao | Security groups for FL pods (no traffic from Cowrie; DNS, S3, coordinator allowed) |
| IAM / S3 owner | IRSA role per org for the `fl/fl-client` service account |
| Jun Hao | VPCs, NAT gateways and their public IPs for the coordinator allowlist |
| Zhi Yu | FL client manifests and overlays, coordinator ECS task, end-to-end check |

## Consequences

- Cowrie manifests and Mervyn's overlays are unchanged; FL lives in
  `deploy/k8s/base/fl-client`, `deploy/k8s/overlays/local/fl/` and `deploy/ecs`.
- Pending: AWS client overlays (ECR image, S3 key, TLS, Fargate-sized requests),
  IRSA/IAM templates, and the ECS task and service for the coordinator.
