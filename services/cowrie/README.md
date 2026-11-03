# Cowrie configuration

See [the deployment guide](../../docs/cowrie-kubernetes.md) for rendering, local
Secrets, prerequisites, validation and cleanup.

- `cowrie.cfg` is the common configuration. AWS organisation patches restore the
  backed-up `orga-lab`, `orgb-lab` and `orgc-lab` hostnames.
- `upload.sh` preserves the backup's per-pod log upload layout and symlink/test-file
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
separately for each organisation. Each organisation now has its own EKS cluster
using the default namespace. Keep decoy passwords separate from real accounts.

Each replica writes to its own pod-UID subdirectory on the EFS PVC. The uploader
reads the entire same claim, including directories left by deleted pods, and does
not delete source logs. The updated HPA enables scale-down with a 300-second
stabilization window and at most one pod removed per 60 seconds. This policy is
untested on a live cluster. Scale-down can disconnect active SSH sessions because
CPU-based HPA does not check session activity; see the guide's manual test.

This extraction does not add an application image build or a local-cluster overlay.
