# 0001: Separate Cowrie clusters per organisation

Status: accepted AWS target configuration; EKS deployment not verified. Date: 3 November 2026.

Subsequent local kind deployment and S3 upload evidence is recorded in
[the local guide](../cowrie-local.md); it does not establish an EKS deployment.

The recovery baseline placed orga, orgb and orgc in namespaces on one EKS cluster.
The target architecture uses `orga-honeypot-cluster`, `orgb-honeypot-cluster` and
`orgc-honeypot-cluster`, with Cowrie in `default` in each. No custom Namespace
resources or aggregate deployment target remain.

Shared source manifests are reused through independent overlays. Organisation
labels and hostnames, EFS access points, IAM roles and S3 prefixes retain identity.
Uploader identity comes from its pod label; Pod Identity trust and associations
bind the role to the organisation's cluster ARN and `default` service account.
Every operational kubectl invocation selects a fixed explicit context and default
namespace, with a local cluster-ARN check and organisation-bound manifest paths.

This separates control-plane/resource namespaces and requires independent worker
capacity, add-ons, operator access and lifecycle management. A shared filesystem
with distinct access points and S3 bucket with distinct prefixes may remain; VPC
isolation, IAM enforcement and data cutover still need infrastructure-owner review.
No cluster creation/deletion or existing installation migration is performed by
this repository change. Backups remain immutable reference snapshots.

Both HPA scaling directions are configured; EKS scale-down and the three-cluster
architecture still require live verification. CPU HPA does not inspect SSH session activity.
See [the guide](../cowrie-kubernetes.md) for validation and migration procedures.
