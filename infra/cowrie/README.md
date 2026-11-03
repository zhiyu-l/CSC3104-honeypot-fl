# Cowrie infrastructure inputs for three clusters

Each organisation uses its own EKS cluster and the built-in `default` namespace:

| Organisation | Cluster / kubectl context alias | Pod Identity service account |
| --- | --- | --- |
| orga | orga-honeypot-cluster | default/cowrie-log-uploader |
| orgb | orgb-honeypot-cluster | default/cowrie-log-uploader |
| orgc | orgc-honeypot-cluster | default/cowrie-log-uploader |

These JSON files are **templates**, not a complete Terraform/CloudFormation stack.
No configuration command provisions a cluster, changes IAM or creates an association.
Coordinate all AWS changes with the infrastructure owner.

## IAM and Pod Identity

Keep one uploader role per organisation. The renderer uses local settings to
produce per-cluster inputs under `.local/cowrie/rendered/<org>/infra/`:

- `pod-identity-association.json`: exact cluster name, namespace `default`, service
  account `cowrie-log-uploader`, and that organisation's role ARN. Source templates
  live in the matching `infra/cowrie/<org>/` directory.
- `pod-identity-trust.json`: trusts `pods.eks.amazonaws.com` for AssumeRole and
  TagSession, constrained by the exact EKS cluster ARN, `default` namespace and
  uploader service account. Keep Pod Identity session tags enabled.
- `uploader-policy.json`: permits PutObject and AbortMultipartUpload only under
  `s3://<bucket>/<org>/raw/*`. No cross-organisation reads/deletes are granted.

The common `pod-identity-trust.template.json` uses `default`; replace the cluster
ARN placeholder only with the selected organisation's actual ARN. Organisation
identity can no longer be inferred from namespace, so the cluster ARN condition
is essential. Role ARNs in local settings must be distinct and belong to their
cluster's account/partition. No static credentials or IRSA annotations are added.

Pod Identity associations are EKS resources, not Kubernetes RBAC or ServiceAccount
annotations. For an approved **future** association setup, the infrastructure owner
can use the selected cluster's rendered API input, for example in PowerShell:

```powershell
$org = 'orga' # Repeat separately for orgb and orgc with the appropriate AWS identity.
$settings = Get-Content -Raw .local/cowrie/settings.json | ConvertFrom-Json
aws eks create-pod-identity-association --region $settings.aws_region --cli-input-json "file://.local/cowrie/rendered/$org/infra/pod-identity-association.json"
```

The input itself specifies `orga-honeypot-cluster`, `orgb-honeypot-cluster` or
`orgc-honeypot-cluster`; never submit the old shared-cluster association exports.
Check for an existing association and reconcile it through the owner's stack
instead of creating duplicates. No AWS command above was run during preparation.
Install/review IAM policies through that stack before associating a role. For
SSE-KMS, add only the required permissions on the chosen KMS key and in its key
policy. Bucket policies must permit the selected roles, especially across accounts.

## Prerequisites per cluster

- EKS cluster, worker capacity and network ownership independent of the other
  organisations; EKS Auto Mode NLB support, or a deliberately adapted Service.
- Metrics Server/resource metrics API for HPA in each cluster.
- EFS CSI driver/IAM and reachable mount targets, DNS, routes and TCP 2049 access
  from that cluster's workers. A common filesystem may remain shared, but retain
  three distinct access points rooted at `/orga`, `/orgb` and `/orgc`. Recovery
  POSIX settings were UID/GID `999:999`, root ownership `999:999`, permissions `750`.
  If reusing these access points, coordinate old/new writers and uploader schedules.
- Pod Identity support/agent on the selected worker type, node permissions and
  one association for `default/cowrie-log-uploader` in the correct cluster. Keep
  these permissions off Cowrie's default service account.
- S3 public-access blocking, encryption, retention/lifecycle rules and egress to
  image registries, S3, STS and EKS authentication endpoints as needed.
- Network isolation from FL/training services; a separate cluster alone does not
  configure VPC routing, security groups or pod egress restrictions.

No application Role/RoleBinding is needed: neither workload calls the Kubernetes
API. Any future service-account subject must use `namespace: default` in its own
cluster, and operator RBAC access must be configured separately per cluster.
Do not copy broad old cluster RBAC/access grants into these templates.

The backup's `describe-*.json`, `eks-*.json`, `efs-*.json`, `addon-*.json`,
`access-*.json` and `pod-identity-*.json` are **describe-output reference snapshots**,
not deployable infrastructure or valid create-request bodies. Their old cluster
name, namespace associations, IDs, timestamps, state and ARNs must not be replayed.
The original backups remain unchanged outside Git. Capture new cluster/network,
EFS, bucket, IAM and association resources in the team's infrastructure stack.

All Kubernetes operations must use the context-checked wrapper documented in
[the deployment guide](../../docs/cowrie-kubernetes.md); it always passes an
explicit organisation context and `--namespace default`.

References: [EFS](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html),
[Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html),
[trust conditions](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-role.html),
[NLB configuration](https://docs.aws.amazon.com/eks/latest/userguide/auto-configure-nlb.html).
