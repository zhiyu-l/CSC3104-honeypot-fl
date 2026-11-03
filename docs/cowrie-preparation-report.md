# Cowrie GitHub preparation report

Prepared 3 November 2026. Preparation and validation did not deploy AWS or
Kubernetes resources. GitHub publication is separate from deployment.
The initial repository had only placeholders in
the relevant implementation folders. Existing unrelated files were preserved.

The HPA was subsequently updated for all three organisations to support automatic
scale-down: a 300-second stabilization window, `selectPolicy: Min` and a maximum
of one pod removed per 60 seconds. Replica bounds, CPU target and scale-up settings
are unchanged. The previously tested recovery configuration supported scale-up
only; **this new scale-down configuration has not been tested on a live cluster**.

## Changed files

Modified: `.gitignore`, `README.md`.

Removed placeholder files: `deploy/k8s/base/.gitkeep`,
`deploy/k8s/overlays/aws/.gitkeep`, `infra/.gitkeep`, `services/cowrie/.gitkeep`.

Added:

```text
.gitattributes  (preserves LF for the ConfigMap-mounted shell script on Windows)
deploy/k8s/
  cowrie-requirements.txt
  cowrie_config.py
  base/cowrie/
    deployment.yaml
    hpa.yaml
    kustomization.yaml
    pvc.yaml
    upload-cronjob.yaml
    uploader-serviceaccount.yaml
  overlays/aws/cowrie/
    README.md
    kustomization.yaml
    settings.example.json
    orga/  (all seven files below)
    orgb/  (all seven files below)
    orgc/  (all seven files below)
      config.patch.yaml
      kustomization.yaml
      namespace.yaml
      pv.template.yaml
      pvc.patch.yaml
      service.template.yaml
      upload-settings.template.yaml
docs/
  cowrie-kubernetes.md
  cowrie-preparation-report.md
infra/cowrie/
  README.md
  pod-identity-trust.template.json
  uploader-policy.template.json
services/cowrie/
  README.md
  cleanup.py
  cowrie.cfg
  kustomization.yaml
  secrets.example.yaml
  upload.sh
  userdb.example.txt
```

## Checks performed

- Kustomize builds and reference validation passed for 36 resources across all
  three namespaces. ConfigMap hashes/references, namespace ownership, volume
  mounts, service accounts, PV/PVC reservations, selectors and ports were checked.
- Source YAML parsed with duplicate-key rejection; JSON, Python syntax and Cowrie
  INI parsed successfully. The uploader shell script passed Bash syntax checking.
- All 36 resources passed offline structural validation against the official
  Kubernetes `v1.37.0` OpenAPI schema, with `IntOrString` normalization.
- Synthetic settings rendered successfully in a temporary directory and the
  output passed validation. No real local settings or Secrets were generated.
- Negative checks rejected unresolved placeholders, shared EFS access points,
  unrestricted CIDRs, stale claim UIDs and missing ConfigMaps. Updated checks
  rejected disabled/faster scale-down, changed scale-up, shared replica log
  directories, a pod-specific uploader subpath and a different uploader claim.
  They require the exact new scale-down policy and unchanged scale-up settings.
- HPA replica bounds, CPU target and scale-up settings, Deployment replica counts,
  Cowrie/cleanup ConfigMap contents and functional Service annotations were
  compared with the backups. Only the requested HPA scale-down behavior changed.
- Every organisation's manifest was checked for pod-UID EFS directories, directory
  creation on the same claim and a full-root read-only mount for the uploader.
  Local uploader execution with synthetic current/deleted-pod directories and a
  stub AWS command verified both are copied without removing source logs.
- Publishable files were checked against the encoded and decoded backup Secret
  values, private-key/AWS-key/token patterns and the source environment's AWS IDs,
  account IDs, source IPs, bucket and cluster name. No matches were found.
- Ignore rules covered local settings, generated Secrets, AWS/kube credentials,
  SSH/PEM keys, environment files and backup exports; examples remain publishable.
- `git diff --check` passed. SHA-256 hashes of all 40 original recovery files
  matched the pre-extraction inventory.

These are offline checks, not proof of runtime behavior. A live cluster is still
needed for admission/server dry runs, PVC binding and EFS access, pod startup and
SSH behavior, HPA metrics/scaling, Pod Identity/IAM, S3 uploads, isolation and NLB
source filtering. HPA scale-down is enabled in the templates but untested live;
upload CronJobs remain suspended. CPU-based HPA does not check active SSH sessions
and can disconnect them during scale-down. See [the deployment guide](cowrie-kubernetes.md)
for prerequisites, deployment order, validation, activation, cleanup and the manual
load/scale-up, idle/scale-down, EFS retention and subsequent S3 upload test.
