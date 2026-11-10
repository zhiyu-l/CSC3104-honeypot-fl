# Cowrie configuration

See [the deployment guide](../../docs/cowrie-kubernetes.md) for rendering, local
Secrets, prerequisites, validation and cleanup.
The AWS nightly overlay runs the combined Python processor instead of `upload.sh`;
see [the EKS runbook](../../docs/cowrie-eks-nightly.md) for its image and checkpoints.

- `cowrie.cfg` is the common configuration. AWS organisation patches restore the
  backed-up `orga-lab`, `orgb-lab` and `orgc-lab` hostnames.
- `upload.sh` writes stable `<org>/raw/by-pod/<pod-uid>/<filename>` object keys,
  replacing the old timestamped snapshot layout. It preserves symlink/test-file
  exclusions. Bucket and region come from `cowrie-upload-settings`; organisation
  comes from the pod organisation label, independently of namespace. It never
  deletes source logs.
- `cleanup.py` is the backed-up **dry-run** retention checker. It prints candidates
  only and is packaged as `efs-cleanup-check`; no workload runs it automatically.
- `kustomization.yaml` generates ConfigMaps with content hashes. Kustomize updates
  workload references automatically; do not apply this directory independently.
- `userdb.example.txt` and `secrets.example.yaml` contain placeholders only. They
  are documentation examples, excluded from the Kustomize resource lists.

The Deployment retains the backed-up Cowrie image digest, resource limits,
UID-based EFS log directories, Secret mounts and disabled service-account token
mounting. Host keys are shared by replicas within one organisation and generated
separately for each organisation. The AWS target is a separate EKS cluster per
organisation; the local overlays use separate kind clusters. Both use `default`.
Repository configuration alone does not prove EKS deployment. Keep decoy passwords
separate from real accounts.

Each replica writes to its own pod-UID subdirectory on the EFS PVC. The uploader
reads the entire same claim, including directories left by deleted pods, and does
not delete source logs. The updated HPA enables scale-down with a 300-second
stabilization window and at most one pod removed per 60 seconds. This policy is
not yet fully verified on EKS. Local validation evidence is recorded in the local
guide. Scale-down can disconnect active SSH sessions because
CPU-based HPA does not check session activity; see the guide's manual test.

For the separate local kind overlays and team setup, see
[the local guide](../../docs/cowrie-local.md). Cowrie itself uses its existing
image; the combined nightly processor has a separate image build.
