# AWS inputs for the local kind lab

These old bucket/IAM-user policies remain specific to the local lab. For account
`909891185696`, use the AWS renderer's per-organisation uploader/reader policies
and [EKS runbook](../../../docs/cowrie-eks-nightly.md), including KMS key settings.

These files document the setup and provide identity permission policies.
They do not create buckets, users, role trust relationships or credentials.
Do not deploy the EKS Pod Identity templates against kind.

The policies also include `<org>/processed/replay/*`: PutObject for each scoped
uploader and GetObject for the team reader. These are source policy changes;
the AWS owner must update the live IAM policies before using the
[replay feature Job](../../../docs/cowrie-feature-processing.md).

The combined [nightly workflow](../../../docs/cowrie-nightly.md) additionally uses
`<org>/processed/sessions/*`, included in the latest policy files. Apply this
addition to live IAM even if the earlier replay-only policies were already applied.

## Buckets

| Organisation | Bucket | Allowed upload prefix | Region |
| --- | --- | --- | --- |
| orga | csc3104-cowrie-orga | orga/raw/, orga/processed/replay/, orga/processed/sessions/ | ap-southeast-1 |
| orgb | csc3104-cowrie-orgb | orgb/raw/, orgb/processed/replay/, orgb/processed/sessions/ | ap-southeast-1 |
| orgc | csc3104-cowrie-orgc | orgc/raw/, orgc/processed/replay/, orgc/processed/sessions/ | ap-southeast-1 |

Use the existing buckets only with the owner's authorisation. To recreate the
lab in another account, create three globally unique general-purpose bucket names,
then update the local organisation Kustomizations and policy resource ARNs.
Bucket creation settings used in the walkthrough: Singapore region, bucket-owner
enforced ownership (ACLs disabled), all four Block Public Access controls enabled,
SSE-S3 default encryption, versioning disabled and Object Lock disabled.
These are intended settings, not an automated audit of the live buckets.
Keep buckets private; a public bucket policy is not required for same-account
uploaders. No lifecycle expiration is configured here. Agree on retention before
adding any rule that deletes project logs. Stable `raw/by-pod/` keys replace current
objects on each upload; versioning retains old versions if enabled. Legacy dated
snapshots are not automatically removed. See [the ML handoff](../../../docs/cowrie-ml-handoff.md).

## Uploader policies

Attach each `uploader-<org>.policy.json` only to that organisation's dedicated
machine identity. The existing lab uses IAM users named `cowrie-uploader-<org>`
and local AWS profiles named `cowrie-<org>`. The policies allow PutObject under
the matching raw, processed/replay and processed/sessions prefixes, and AbortMultipartUpload under
the raw prefix. They do not grant
read, list, delete, IAM administration or cross-organisation upload access.
Additional policies on an identity can widen its permissions; review the complete
identity configuration. SSE-KMS would require additional scoped key permissions;
these policies assume SSE-S3.

Keep access keys outside Git and outside chat. Use a separate identity for each
team member; do not distribute the original operator's uploader keys. Rotate any
exposed key in IAM and refresh the affected cluster's credential Secret. Changing
the AWS profile does not automatically update an existing Kubernetes Secret.
Upcoming Jobs use the updated Secret; already-running Jobs retain their environment.

## Human team access

`team-log-reader.policy.json` permits listing these three buckets and downloading
raw JSON logs and processed session/replay outputs under the organisation prefixes. It grants no upload/delete or
infrastructure-management permissions. It omits account-wide ListAllMyBuckets;
use a direct bucket console link or `aws s3 ls s3://<bucket>/<org>/raw/by-pod/`.
The policy still covers `raw/*` for compatibility with historical logs; consumers
must select `raw/by-pod/` explicitly to avoid mixing layouts.

Prefer an IAM Identity Center group/permission set for teammates. An account
administrator can use the reader policy as that permission set's inline policy,
assign the group to the project account and give each teammate their own login.
Alternatively, attach it to a dedicated team role, with a trust policy scoped to
the actual authorised identities. Cross-account users also need AssumeRole
permission in their source account. No trust policy is supplied here because the
team's identities/accounts have not yet been specified.

Reader access does not permit provisioning EKS, IAM or S3 infrastructure. Define
maintainer duties before adding those permissions; do not attach AdministratorAccess
just to let a teammate download logs. Human SSO/assumed-role credentials expire:
exporting them into a Kubernetes Secret does not provide automatic renewal.

For the lab's simpler console-login alternative, create one IAM user per teammate,
enable console access with a temporary password and required password change,
and attach this reader policy. Share their individual sign-in details securely.
For an external ML application, use its own read-only IAM identity and access key
or a supported temporary-credential mechanism. Console passwords are not API
credentials; configuring a host AWS profile does not configure a container.
Do not reuse the upload-only users for ML reads. Full console navigation or MFA
self-management may require separately scoped identity permissions beyond this
S3-only reader policy.

See [local setup and credential helper](../../../docs/cowrie-local.md).

References: [Identity Center account assignments](https://docs.aws.amazon.com/singlesignon/latest/userguide/assignusers.html),
[role delegation](https://docs.aws.amazon.com/IAM/latest/UserGuide/tutorial_cross-account-with-roles.html),
[S3 bucket creation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html).
