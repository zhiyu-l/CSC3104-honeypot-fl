# AWS Cowrie templates

**Do not apply these overlays directly.** `kubectl kustomize` builds them for
review, but EFS handles, region, bucket and source CIDRs contain placeholders.
Use `deploy/k8s/cowrie_config.py render` with ignored local settings, as described
in [the guide](../../../../../docs/cowrie-kubernetes.md).

Each organisation overlay adds its namespace, labels, hostname, reserved static
EFS PV/PVC pairing, upload settings and SSH NLB Service to the shared base. The
top-level Kustomization combines all three without renaming shared workloads.

`settings.example.json` documents the local input structure. Role ARNs are inputs
for the external Pod Identity association instructions; the renderer validates
them but does not insert IRSA annotations or create AWS associations.

Only files under `.local/cowrie/rendered/` are applicable outputs after replacing
all settings, reviewing infrastructure prerequisites and generating Secrets.
Applying `services.yaml` can provision AWS NLBs; it is a separate final step.
