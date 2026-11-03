# Cowrie on three EKS clusters

The deployment targets are separate clusters. Each uses the existing `default`
namespace; organisation identity is carried by labels and configuration.

| Organisation | EKS cluster | Required kubectl context alias | Namespace | S3 prefix |
| --- | --- | --- | --- | --- |
| orga | orga-honeypot-cluster | orga-honeypot-cluster | default | orga/raw/ |
| orgb | orgb-honeypot-cluster | orgb-honeypot-cluster | default | orgb/raw/ |
| orgc | orgc-honeypot-cluster | orgc-honeypot-cluster | default | orgc/raw/ |

The original recovery snapshots describe one shared cluster with three organisation
namespaces. They remain unchanged outside Git. These new manifests are a migration
target, not proof that any new cluster exists or that migration has occurred.

## Source and generated files

- `services/cowrie/`: shared Cowrie config, upload script, dry-run cleanup checker
  and placeholder Secret examples. Per-organisation hostname patches remain.
- `deploy/k8s/base/cowrie/`: shared Deployment, HPA, PVC, uploader ServiceAccount
  and suspended upload CronJob.
- `deploy/k8s/overlays/aws/cowrie/{orga,orgb,orgc}/`: independent cluster overlays,
  labels, hostname, static PV reservation, upload settings and SSH NLB templates.
- `infra/cowrie/`: IAM trust/permission templates and one Pod Identity association
  template per named cluster, all targeting service account
  `default/cowrie-log-uploader`.
- `.local/cowrie/`: ignored settings, Secrets and per-cluster rendered outputs.

There are **33 resources, 11 per cluster**: Deployment, HPA, PVC, PV, Service,
ServiceAccount, CronJob and four ConfigMaps. There are no Namespace manifests and
no combined top-level Kustomization: these resources have identical names in
`default` and must never be applied together to one cluster. No application Role,
RoleBinding or cluster-wide RBAC is required by these workloads. Cowrie does not
mount a service-account token; the uploader uses AWS Pod Identity, not Kubernetes
API permissions. Do not restore broad cluster RBAC from the backups.

AWS overlays contain placeholders and are **not directly applicable**. Completed
Jobs, pod inventories, generated CA ConfigMaps, default service accounts, real
Secrets and AWS describe-output snapshots are excluded.

## Prerequisites and settings

Use Python 3.10+, kubectl with Kustomize, and OpenSSH for local keys. Commands run
from the repo root; examples use PowerShell except the marked Bash block.

```powershell
python -m pip install -r deploy/k8s/cowrie-requirements.txt
New-Item -ItemType Directory -Force .local/cowrie | Out-Null
Copy-Item deploy/k8s/overlays/aws/cowrie/settings.example.json .local/cowrie/settings.json
```

Fill every placeholder in settings. Keep the fixed cluster names/context aliases;
provide each actual EKS cluster ARN, its own uploader role ARN, a distinct EFS
access point and approved source IPv4 CIDRs. Region, bucket and EFS filesystem
remain common settings; access points, IAM roles and S3 prefixes remain separate.
Each uploader role must belong to its cluster's AWS account/partition. Establish
EFS and S3 cross-account permissions separately if using different accounts.
Never put passwords, AWS access keys or tokens into settings or version control.

The infrastructure owner must provide all **three** clusters, worker capacity,
Metrics Server, EFS CSI/IAM, Pod Identity support/agent and role associations,
network isolation, EFS mount targets/access and S3 storage. See
[the infrastructure notes](../infra/cowrie/README.md). These files do not provision
clusters, network resources or a complete AWS stack. The Service retains EKS Auto
Mode class `eks.amazonaws.com/nlb`, internet-facing IP targets, cross-zone balancing
and TCP health checks; adapt deliberately for another controller. Applying the
Service later can create an NLB. Check approved source CIDRs and isolation first.

## Explicit contexts and guarded operations

After clusters exist and operator authentication is configured, register the three
context aliases. These are **future operator setup commands**, not provisioning:

```powershell
$settings = Get-Content -Raw .local/cowrie/settings.json | ConvertFrom-Json
aws eks update-kubeconfig --region $settings.aws_region --name orga-honeypot-cluster --alias orga-honeypot-cluster
aws eks update-kubeconfig --region $settings.aws_region --name orgb-honeypot-cluster --alias orgb-honeypot-cluster
aws eks update-kubeconfig --region $settings.aws_region --name orgc-honeypot-cluster --alias orgc-honeypot-cluster
```

Use the appropriate AWS profile/SSO identity for each account. The uploader role
is not an operator authentication role. `update-kubeconfig` can change the active
context; all operations below ignore that implicit selection.

Use the wrapper for **every** operational command:

```powershell
python deploy/k8s/cowrie_config.py check-context --org orga
python deploy/k8s/cowrie_config.py check-context --org orgb
python deploy/k8s/cowrie_config.py check-context --org orgc
python deploy/k8s/cowrie_config.py kubectl --org orga -- get pods
python deploy/k8s/cowrie_config.py kubectl --org orgb -- get pods
python deploy/k8s/cowrie_config.py kubectl --org orgc -- get pods
```

For example, `--org orga` always executes `kubectl --context orga-honeypot-cluster
--namespace default ...`. The wrapper first reads that context's local kubeconfig
cluster reference and requires the exact configured EKS ARN. It refuses context,
namespace, cluster, user, server and kubeconfig overrides. File operations accept
only the selected organisation's rendered YAML or local Secret files; target ARN,
labels and namespaces are checked before invoking a cluster command. Combined,
foreign-organisation, template and standard-input manifest paths are rejected.
The check does not independently authenticate a kubeconfig server address or prove
that the remote cluster is healthy; protect kubeconfig and verify it against AWS
through the infrastructure owner. No script changes `current-context`.

Offline Kustomize builds also specify the organisation's context and `default`
namespace explicitly, but do not read cluster state or require working credentials.

## Render and validate locally

```powershell
python deploy/k8s/cowrie_config.py validate
python deploy/k8s/cowrie_config.py render
python deploy/k8s/cowrie_config.py validate --rendered
# Optional single-cluster operation:
python deploy/k8s/cowrie_config.py validate --org orga
```

Render/validate default to all three targets **independently**. The renderer writes
`.local/cowrie/rendered/<org>/storage.yaml`, `configuration.yaml`, `workloads.yaml`,
`services.yaml`, `target.json` and `infra/*.json`. It never contacts AWS or a
cluster. `target.json` binds an output directory to its organisation, EKS ARN,
context and namespace. There is no `namespaces.yaml`. Old combined outputs from
the shared-cluster design are obsolete; the wrapper refuses them. Inspect them
and retire them locally instead of applying them. Render again after any source
or setting change; existing files are not refreshed automatically.

The renderer rejects placeholders, mismatched contexts/cluster ARNs, duplicate EFS
access points/roles and unrestricted `/0` CIDRs. Local validation checks references,
labels, default namespaces, PV reservations, selectors/ports, HPA behavior, UID log
mounts and the uploader's full-root mount. Optional offline structural API checks:

```powershell
python deploy/k8s/cowrie_config.py validate --schema <path-to-official-swagger.json>
python -m unittest discover -s deploy/k8s/tests -v
```

Use the official Kubernetes `swagger.json` for the target version outside Git.
The validator normalizes OpenAPI `IntOrString`; admission, IAM, EFS, metrics and
application behavior still require a live cluster.

## Generate local Secrets

The Secret examples contain placeholders only and target `default`. Never apply
them. Keep distinct host keys and decoy login data for each organisation, even
though the resource names are the same in all three clusters. Base64 is sensitive.
If retaining an old SSH fingerprint, recover the keys securely outside Git.

In **Bash**, after configuring settings/context aliases, generate local files only:

```bash
set -euo pipefail
umask 077
tool=deploy/k8s/cowrie_config.py
for org in orga orgb orgc; do
  python "$tool" check-context --org "$org"
  dir=".local/cowrie/secrets/$org"
  mkdir -p "$dir"
  for algorithm in rsa ecdsa ed25519; do
    if [ -e "$dir/ssh_host_${algorithm}_key" ]; then
      echo "Existing key set for $org; inspect before regenerating" >&2
      exit 1
    fi
  done
  ssh-keygen -q -t rsa -b 3072 -N "" -f "$dir/ssh_host_rsa_key"
  ssh-keygen -q -t ecdsa -b 256 -N "" -f "$dir/ssh_host_ecdsa_key"
  ssh-keygen -q -t ed25519 -N "" -f "$dir/ssh_host_ed25519_key"
  cp services/cowrie/userdb.example.txt "$dir/userdb.txt"
  "${EDITOR:-vi}" "$dir/userdb.txt"
  if grep -q REPLACE_ "$dir/userdb.txt"; then
    echo "Replace decoy userdb placeholders before continuing" >&2
    exit 1
  fi
  python "$tool" kubectl --org "$org" -- create secret generic cowrie-host-keys \
    --from-file="$dir/ssh_host_rsa_key" --from-file="$dir/ssh_host_rsa_key.pub" \
    --from-file="$dir/ssh_host_ecdsa_key" --from-file="$dir/ssh_host_ecdsa_key.pub" \
    --from-file="$dir/ssh_host_ed25519_key" --from-file="$dir/ssh_host_ed25519_key.pub" \
    --dry-run=client -o yaml > "$dir/host-keys.secret.yaml"
  python "$tool" kubectl --org "$org" -- create secret generic cowrie-userdb \
    --from-file="$dir/userdb.txt" --dry-run=client -o yaml > "$dir/userdb.secret.yaml"
done
```

The wrapper reads local kubeconfig and `--dry-run=client` generates Secret YAML;
it does not create cluster Secrets. Restrict filesystem access, including Windows
ACLs where needed. Never reuse a real password or paste generated Secrets into
issues/reports. After rotating mounted Secrets/configuration, deliberately restart
Cowrie: its `subPath` file mounts do not refresh inside an existing pod.

## Deployment order and migration (future manual operations)

No command in this section has been executed during preparation. Choose **one**
organisation per terminal and repeat the procedure separately for all three:

```powershell
$org = 'orga' # Select orgb or orgc explicitly in the corresponding terminal.
$tool = 'deploy/k8s/cowrie_config.py'
$rendered = ".local/cowrie/rendered/$org"
python $tool check-context --org $org
```

1. Coordinate infrastructure and ownership. Review the existing `default`
   namespace for collisions: these workloads are named `cowrie`, `cowrie-hpa`,
   `cowrie-logs`, `cowrie-log-uploader` and `cowrie-logs-to-s3` in every cluster.
   Apply network isolation before exposing Cowrie to traffic. For migration,
   coordinate stopping old writers/upload schedules and traffic cutover with the
   old cluster's owner. Do not mutate or delete the old installation/backups as
   part of preparing these files. Reusing the same organisation access point
   preserves old UID log directories; overlapping uploaders can duplicate objects.
2. The built-in `default` namespace already exists. Apply storage, wait for the
   PVC to bind to `cowrie-logs-<org>`, then apply Secrets and configuration:

   ```powershell
   python $tool kubectl --org $org -- apply --dry-run=server -f "$rendered/storage.yaml"
   python $tool kubectl --org $org -- apply -f "$rendered/storage.yaml"
   python $tool kubectl --org $org -- get pv
   python $tool kubectl --org $org -- get pvc cowrie-logs
   Get-ChildItem ".local/cowrie/secrets/$org" -Filter *.secret.yaml | ForEach-Object { python $tool kubectl --org $org -- apply -f $_.FullName }
   python $tool kubectl --org $org -- apply --dry-run=server -f "$rendered/configuration.yaml"
   python $tool kubectl --org $org -- apply -f "$rendered/configuration.yaml"
   ```

   PV `claimRef` is `default/cowrie-logs` with no old UID/resourceVersion. PVC
   `volumeName` remains organisation-specific. Empty storage classes preserve
   static provisioning, RWX EFS and TLS are retained, and `5Gi` is a binding value,
   not an EFS quota. Existing stale live PV bindings need owner review; do not
   clear a live claim UID or adopt someone else's volume automatically.
3. Have the infrastructure owner install the rendered per-cluster Pod Identity
   trust/permission policy and association under `$rendered/infra/`. The association
   specifies the exact cluster name, namespace `default`, service account
   `cowrie-log-uploader` and that organisation's role ARN. Role ARNs in settings
   do not automatically create associations. See the infrastructure instructions;
   no IRSA annotations or static AWS credentials are added.
4. Apply workloads and inspect the organisation's pods and metrics:

   ```powershell
   python $tool kubectl --org $org -- apply --dry-run=server -f "$rendered/workloads.yaml"
   python $tool kubectl --org $org -- apply -f "$rendered/workloads.yaml"
   python $tool kubectl --org $org -- rollout status deployment/cowrie
   python $tool kubectl --org $org -- get pods,hpa,cronjobs
   python $tool kubectl --org $org -- top pods -l app=cowrie
   ```

5. Confirm isolation, approved CIDRs and NLB settings before applying the Service:

   ```powershell
   python $tool kubectl --org $org -- apply --dry-run=server -f "$rendered/services.yaml"
   python $tool kubectl --org $org -- apply -f "$rendered/services.yaml"
   python $tool kubectl --org $org -- get service cowrie-ssh
   ```

   Test SSH on the resulting hostname's port 2222 from an approved source and
   verify a disallowed source is blocked. Inspect events without dumping Secrets.

## Persistent logs and uploader identity

Both Cowrie and its init container read `POD_UID` from `metadata.uid`. The init
container creates `/efs/<pod-uid>` on the PVC with `mkdir -p`; Cowrie mounts that
same directory at `/cowrie/cowrie-git/var/log/cowrie` using
`subPathExpr: $(POD_UID)`. Every replica/replacement gets a different directory.
The uploader mounts the whole same PVC read-only at `/logs`, without a subpath,
and scans `/logs/*/cowrie.json*` including deleted pods' directories. PVs use
`Retain`; pod deletion does not remove the directory or PVC. The cleanup checker
is a dry-run ConfigMap with no automated workload. Actual EFS permissions and
log writes must still be verified live.

**Uploader `ORG` now comes from `metadata.labels['organisation']`, not the
namespace.** Each CronJob pod has the appropriate organisation label. This keeps
`orga/raw/`, `orgb/raw/` and `orgc/raw/` separate even though all namespaces are
`default`. IAM allows uploads only to the matching prefix and the trust binds
cluster ARN, `default` namespace and service account. Verify cross-organisation
access is denied using operator-controlled tests.

The CronJob remains suspended, runs at midnight `Asia/Singapore` when enabled,
uses `Forbid` concurrency, one-hour start/execution deadlines, three retries and
history limits of three. A manual Job runs while the CronJob is suspended:

```powershell
$job = 'cowrie-upload-' + (Get-Date -Format 'yyyyMMddHHmmss')
python $tool kubectl --org $org -- create job --from=cronjob/cowrie-logs-to-s3 $job
python $tool kubectl --org $org -- wait --for=condition=complete "job/$job" --timeout=3600s
python $tool kubectl --org $org -- logs "job/$job"
```

Generate benign logs first. Verify STS identity and objects under
`s3://<bucket>/<org>/raw/<UTC-date>/`. Names include upload timestamp, uploader UID,
source pod UID and original filename. Uploads skip symlinks/pipeline-test files,
fail on no matches, include active/rotated files and never delete source logs.
Active files may change during copying; uploads are not a consistent snapshot or
deduplicated archive. Operator credentials must verify contents because uploader
roles cannot read/delete objects. Do not approve EFS deletion from copy success.

Enable scheduling only after live validation in a reviewed source change (and
update the validator's expected suspension policy). A temporary operator trial
can use the guarded wrapper with `patch cronjob cowrie-logs-to-s3 --type=merge`
and a patch file, but reapplying the current source suspends it again. Unsuspending
may immediately schedule a missed run within its start deadline.

## HPA and manual scale test

The historical recovery baseline tested scale-up only (`scaleDown: Disabled`).
The repository now enables **both directions in all three cluster overlays**;
scale-down and this three-cluster architecture are **untested on a live cluster**.
Min/max replicas remain 1/3 and CPU target 60%. Scale-up retains `Max` of four
pods or 100% per 15 seconds and zero stabilization. Scale-down uses:

```yaml
scaleDown:
  stabilizationWindowSeconds: 300
  selectPolicy: Min
  policies:
    - type: Pods
      value: 1
      periodSeconds: 60
```

With sustained low CPU, the controller considers the highest recommendation in
the trailing 300 seconds and removes at most one replica per 60 seconds. Metrics
and reconciliation can delay this further. **Scale-down can disconnect active
SSH sessions: CPU-based HPA does not check whether sessions are active.** The
30-second grace period is not session draining or a guarantee of flushed logs.

Repeat these four steps separately for each explicitly selected `$org`, after an
approved isolated test deployment. Initialize `$org` and `$tool` as above in each
terminal. Keep uploads suspended. Save evidence in `.local/cowrie/scaling-test/$org/`.

1. **Generate controlled load and observe scale-up.** Let the idle HPA reach one
   replica first: the Deployment starts at three, so starting there cannot show
   scale-up. Check metrics/conditions. From an operator terminal run a bounded
   three-minute CPU load on one Cowrie pod after confirming `python3` is present:

   ```powershell
   $pod = (python $tool kubectl --org $org -- get pods -l app=cowrie -o json | ConvertFrom-Json).items[0].metadata.name
   python $tool kubectl --org $org -- exec $pod -c cowrie -- python3 -c "import time; end = time.monotonic() + 180; exec('while time.monotonic() < end: pass')"
   ```

   In separately initialized terminals observe:

   ```powershell
   python $tool kubectl --org $org -- get hpa cowrie-hpa --watch
   python $tool kubectl --org $org -- get pods -l app=cowrie --watch
   ```

   Generate a benign SSH interaction for each replica using a guarded operator
   port-forward (`python $tool kubectl --org $org -- port-forward pod/<pod-name> 2222:2222`).
   Confirm each replica has a nonempty `cowrie.json`. At high replica count save:

   ```powershell
   $evidence = ".local/cowrie/scaling-test/$org"
   New-Item -ItemType Directory -Force $evidence | Out-Null
   python $tool kubectl --org $org -- get pods -l app=cowrie -o json | Set-Content -Encoding utf8 "$evidence/before.json"
   ```

2. **Stop load and observe CPU drop.** Confirm the bounded load exited and stop
   other test traffic. Sample `python $tool kubectl --org $org -- top pods -l app=cowrie`
   until CPU stays below target. Utilisation is relative to the `100m` request;
   low CPU says nothing about active sessions. Record timestamps.
3. **Wait for stabilization and observe scale-down.** Allow high recommendations
   to age out of the 300-second window plus metric/controller delay. Expect
   `3 -> 2 -> 1`, with no more than one replica removed per 60 seconds. Record
   `python $tool kubectl --org $org -- describe hpa cowrie-hpa` and
   `python $tool kubectl --org $org -- get events --sort-by=.lastTimestamp`.
   Inspect missing metrics/recommendations if it does not shrink; five minutes
   is not an exact deadline. Do not reapply the Deployment during measurement:
   its `replicas: 3` can reset the HPA-managed count. Record disconnected sessions.
4. **Verify retained EFS logs and subsequent upload.** After termination, compare
   pod UIDs with the saved inventory and run a manual uploader Job:

   ```powershell
   $before = Get-Content -Raw "$evidence/before.json" | ConvertFrom-Json
   $after = python $tool kubectl --org $org -- get pods -l app=cowrie -o json | ConvertFrom-Json
   $removed = @($before.items.metadata.uid | Where-Object { $_ -notin $after.items.metadata.uid })
   $removed
   $job = 'cowrie-retention-' + (Get-Date -Format 'yyyyMMddHHmmss')
   python $tool kubectl --org $org -- create job --from=cronjob/cowrie-logs-to-s3 $job
   python $tool kubectl --org $org -- wait --for=condition=complete "job/$job" --timeout=3600s
   python $tool kubectl --org $org -- logs "job/$job"
   ```

   Require at least one removed UID and `Uploaded:` output for its real log file.
   Verify the S3 object name includes that UID and its content contains the expected
   benign events. A successful Job might upload only surviving pods' files, so
   check removed UIDs explicitly. Confirm surviving logs too; retention does not
   prove that no buffered event was lost. Remove the test Job with
   `python $tool kubectl --org $org -- delete job $job` after reviewing evidence.
   Keep EFS source directories and the CronJob suspension intact.

## Cleanup

Initialize the explicit `$org`, `$tool` and `$rendered` target for the cluster being
retired. Suspend scheduling first; stop/finish manual Jobs and verify required
archives. Remove Services and wait for their NLBs/targets to disappear, then remove
workloads/configuration/Secrets owned by this installation:

```powershell
$suspendPatch = ".local/cowrie/$org-suspend.json"
@{spec = @{suspend = $true}} | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 $suspendPatch
python $tool kubectl --org $org -- patch cronjob cowrie-logs-to-s3 --type=merge --patch-file $suspendPatch
python $tool kubectl --org $org -- delete -f "$rendered/services.yaml"
python $tool kubectl --org $org -- delete -f "$rendered/workloads.yaml"
python $tool kubectl --org $org -- delete -f "$rendered/configuration.yaml"
Get-ChildItem ".local/cowrie/secrets/$org" -Filter *.secret.yaml | ForEach-Object { python $tool kubectl --org $org -- delete -f $_.FullName }
```

Only after every pod/Job releases storage, delete the selected PVC and then PV
objects if deliberately retiring them. `Retain` keeps EFS data; EFS access points,
filesystems, S3 contents, IAM associations/roles and KMS keys have separate owner
retention/deletion decisions. **Never delete the `default` namespace.** Do not
recreate/delete clusters as part of this repository migration. Retire infrastructure
through its owner's stack and protect/dispose of local key material appropriately.

Backups remain intact. AWS `describe-*.json`, EKS/EFS/addon/access/identity exports
are reference snapshots, not deployable infrastructure or create-request inputs.
Do not copy them or decoded/base64 Secret data into Git. Existing ignore rules
exclude local configuration, credentials, backups and generated Secrets. Review
tracked and new files for sensitive content before any future publication.

References: [HPA behavior](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/),
[Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html),
[context aliases](https://docs.aws.amazon.com/cli/latest/reference/eks/update-kubeconfig.html),
[EFS prerequisites](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html).
