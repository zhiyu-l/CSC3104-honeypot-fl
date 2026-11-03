# Cowrie AWS prerequisites and IAM templates

These files are **templates**, not a complete Terraform/CloudFormation stack.
Coordinate infrastructure changes with the infrastructure owner. No commands in
the local Cowrie renderer create AWS resources or modify IAM.

Create one uploader IAM role per organisation. Copy the two JSON templates into an
ignored `.local/` directory and replace every `REPLACE_...` value, using `aws` as
the partition for standard AWS regions. Restrict each role to its organisation's
`raw/` prefix. The trust policy binds the role to the intended cluster, namespace
and service account through Pod Identity request tags; keep session tags enabled.
Attach the rendered upload policy to that role. The role ARN goes in local
`settings.json`. This is configuration, not an AWS access key.

The object policy covers `aws s3 cp` uploads and multipart aborts. It grants no log
deletion, object reads or cross-organisation access. For SSE-KMS, separately grant
the required permissions on the chosen KMS key and in its key policy. Apply bucket
public-access blocking, encryption and retention/lifecycle rules through the
infrastructure owner's stack. The uploader's `sts get-caller-identity` diagnostic
does not require adding a broad STS permission policy.

Associate each role externally with `cowrie-log-uploader` in `orga`, `orgb` or
`orgc`. Pod Identity associations live in the EKS API; a Kubernetes ServiceAccount
manifest alone cannot recreate them. The recovery ServiceAccounts had no IRSA role
annotation, so none is invented here. If adopting IRSA instead, configure OIDC,
trust and ServiceAccount annotations explicitly with the infrastructure owner.

Required infrastructure:

- EKS, worker capacity, networking and EKS Auto Mode NLB support (or a deliberately
  adapted Service for another load-balancer controller).
- EFS filesystem and three distinct access points rooted at `/orga`, `/orgb` and
  `/orgc`. The recovery used POSIX UID/GID `999:999`, root ownership `999:999`,
  permissions `750`. Ensure mount targets, DNS, routes and security groups allow
  NFS TCP 2049 from workers; install and authorize the EFS CSI driver.
- Metrics Server and the resource metrics API for CPU-based HPA.
- EKS Pod Identity Agent/support on the selected worker type, appropriate node
  permissions and one uploader role association per namespace. Keep uploader IAM
  permissions off the Cowrie Deployment's default service account.
- Private S3 storage and outbound access to required image registries, S3, STS and
  EKS authentication endpoints, via appropriate endpoints or egress routing.

The backup's `describe-*.json`, `eks-*.json`, `efs-*.json`, `addon-*.json`,
`access-*.json` and `pod-identity-*.json` are **AWS describe-output reference
snapshots**, not deployable infrastructure or valid create-request bodies. Their
IDs, state, timestamps and ARNs belong to the old environment. Do not copy these
snapshots, broad cluster RBAC or old IAM grants into Git. Capture any new network,
EFS, bucket, IAM and association resources in the team's infrastructure stack.

References: [EFS on EKS](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html),
[Pod Identity associations](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html),
[role trust conditions](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-role.html),
[NLB configuration](https://docs.aws.amazon.com/eks/latest/userguide/auto-configure-nlb.html).
