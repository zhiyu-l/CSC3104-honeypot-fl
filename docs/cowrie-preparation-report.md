# Cowrie configuration and architecture report

Updated 3 November 2026. The configuration now targets three separate EKS clusters:
`orga-honeypot-cluster`, `orgb-honeypot-cluster` and `orgc-honeypot-cluster`, each
using `default`. This supersedes the old shared-cluster namespace deployment.
Preparation did not provision or deploy resources; GitHub publication is separate
from deployment.
The original recovery backups remain unchanged outside the repository.

## Changes in this migration

- Removed `deploy/k8s/overlays/aws/cowrie/kustomization.yaml` (the aggregate target)
  and all three `*/namespace.yaml` files. Each overlay now targets `default`;
  static PV claim reservations are `default/cowrie-logs`.
- Updated `deploy/k8s/base/cowrie/upload-cronjob.yaml` to take `ORG` from the
  organisation pod label. S3 prefixes remain orga/orgb/orgc even in `default`.
- Updated `deploy/k8s/cowrie_config.py` for independent builds/validation/rendering,
  per-cluster target metadata, explicit kubectl contexts, kubeconfig ARN checks,
  namespace/override checks and rejection of another organisation's manifests.
- Updated `settings.example.json` with fixed cluster/context names and cluster ARN
  placeholders. EFS access points and uploader roles remain distinct.
- Added `infra/cowrie/{orga,orgb,orgc}/pod-identity-association.template.json`;
  updated common trust to `default` with exact cluster-ARN restriction. Rendering
  creates matching per-cluster association/trust/upload-policy JSON locally.
- Updated Secret examples, README, deployment/infrastructure/service guides and
  added an architecture decision record. Added offline boundary regression tests
  in `deploy/k8s/tests/test_cowrie_config.py`.
- HPA behavior is unchanged from the already prepared scale-down version: min/max
  1/3, CPU 60%, existing scale-up `Max` and zero stabilization, scale-down `Min`
  with 300-second stabilization and one pod per 60 seconds. CronJobs stay suspended.

## Validation

- Independent Kustomize builds: **33 resources, 11 per cluster**, no Namespace
  resources, all namespaced resources in `default`, organisation labels retained.
- Resource identities/references, hashed ConfigMaps, service-account/PVC mounts,
  selectors/ports, PV reservations, HPA and CronJob policy checked locally.
- Official Kubernetes v1.37.0 structural schema checks, with IntOrString handling.
- Synthetic rendering and per-cluster Pod Identity/trust/S3-prefix checks outside
  the repo. No real local settings, private keys or Secrets generated.
- All 13 offline regression tests passed, covering correct context injection, wrong context ARN,
  namespace/cluster overrides, old namespace/claim/uploader identity, mixed
  manifests, shared roles, foreign file paths and stale/tampered target outputs.
  Operational subprocesses are mocked; real kubectl calls only perform local
  Kustomize builds with an explicit context/default namespace.
- UID-based EFS directories and full-root read-only uploader mounts remain checked.
  Local synthetic uploads include current/deleted-pod logs and retain source files.
- YAML duplicate-key parsing, JSON/Python/INI/shell checks, sensitive-content scan,
  ignore coverage, whitespace and recovery SHA-256 checks are included in the
  final local review. Backups are preserved; preparation left the Git index/HEAD
  unchanged before publication.

## Live checks still required

The previously tested recovery configuration supported scale-up only. The new
three-cluster deployment and automatic scale-down have **not been tested live**.
Verify each actual EKS ARN/context, operator access, add-ons, IAM/Pod Identity,
EFS writes/permissions/binding/retention, metrics, network isolation, NLB filtering
and S3 delivery. HPA does not check active SSH sessions; scale-down can disconnect
them and does not guarantee all buffered log events are flushed.

Use [the deployment guide](cowrie-kubernetes.md) for explicit targets, migration
order, local Secrets, manual load/scale-up and idle/scale-down tests, retained
EFS-log/upload verification and cleanup. Never delete the `default` namespace or
replay AWS describe-output snapshots as deployable infrastructure.
