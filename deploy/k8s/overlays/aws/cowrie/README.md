# Independent Cowrie cluster templates

The AWS-only `nightly/` component runs the combined processor with Pod Identity
and persistent EFS checkpoints. See the [EKS runbook](../../../../../docs/cowrie-eks-nightly.md).
The shared base and local kind overlays retain their separate implementations.

**Do not apply these templates directly or combine the organisation overlays.**
Each is a separate cluster's workload in `default`:

| Overlay | Required context / EKS cluster |
| --- | --- |
| `orga/` | `orga-honeypot-cluster` |
| `orgb/` | `orgb-honeypot-cluster` |
| `orgc/` | `orgc-honeypot-cluster` |

The previous aggregate Kustomization and custom Namespace resources are removed.
Labels, hostnames, EFS access points, uploader roles, S3 prefixes and named static
PV/PVC pairings remain organisation-specific. Namespaced workload/service-account
references and PV claim reservations now use `default`. Uploader organisation
identity comes from its pod label, not the namespace.

Copy `settings.example.json` to ignored `.local/cowrie/settings.json` and fill in
actual IDs, ARNs and CIDRs. Keep the specified context aliases and cluster names.
Set a digest-pinned `nightly_image` and per-organisation `s3_bucket`, optional
`kms_key_arn`, UID/GID and `schedule_enabled` (false for a new installation).
Legacy top-level bucket settings are rejected. Preserve enabled schedule settings
when updating an already verified deployment.
`python deploy/k8s/cowrie_config.py render` creates separate outputs under
`.local/cowrie/rendered/{orga,orgb,orgc}/`, plus target metadata and per-cluster
Pod Identity/IAM inputs. It does not provision or deploy anything.
Each output includes a focused `nightly.yaml` for updating just the upload
ConfigMap, service account and CronJob, plus an organisation-scoped reader policy.

Use `python deploy/k8s/cowrie_config.py kubectl --org orga -- <command>` (or the
explicit orgb/orgc selection) for operations. It injects the correct context and
default namespace, verifies the local context-to-cluster ARN mapping and rejects
foreign-organisation manifest paths. Only render/validate default to all three;
operational commands always require `--org`.

See [the guide](../../../../../docs/cowrie-kubernetes.md) for settings, context
aliases, local Secrets, deployment order, live testing and cleanup. Applying a
rendered `services.yaml` later may provision an NLB; the renderer does not do so.
