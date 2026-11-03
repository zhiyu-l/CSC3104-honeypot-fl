# Cowrie Kubernetes configuration

This configuration was extracted from the `cowrie-recovery-20261025T053325Z`
recovery snapshot for `orga`, `orgb` and `orgc`. The original backup stays outside
the repository and is unchanged. Treat it as sensitive: namespace exports contain
base64-encoded Secret data and private SSH host keys.

## Files and scope

| Location | Purpose |
| --- | --- |
| `services/cowrie/` | Cowrie configuration, upload script, dry-run cleanup checker and placeholder Secret examples |
| `deploy/k8s/base/cowrie/` | Shared Deployment, HPA, PVC, uploader ServiceAccount and suspended upload CronJob |
| `deploy/k8s/overlays/aws/cowrie/{orga,orgb,orgc}/` | Namespace, organisation/hostname patches, static PV binding and AWS Service/configuration templates |
| `infra/cowrie/` | Parameterised IAM examples and AWS prerequisite/reference notes |
| `.local/cowrie/` | Ignored settings, local Secrets and rendered applicable YAML |

There are 36 rendered resources: per organisation, one Namespace, Deployment,
HPA, PVC, PV, Service, ServiceAccount and CronJob, plus four ConfigMaps. The four
ConfigMaps are Cowrie config, upload settings, upload script and the dry-run
cleanup checker. The checker is retained for manual inspection; nothing runs it
automatically. Completed Jobs, pod inventories, default ServiceAccounts,
`kube-root-ca.crt`, broad cluster RBAC and all real Secrets are excluded.

The AWS overlays are **templates**, including their `kustomization.yaml` entry
points. A successful Kustomize build does not make the placeholder values usable.
The base is a shared building block and still requires AWS configuration and
local Secrets. No working local-cluster deployment is supplied.

## Prerequisites

Use Python 3.10+, `kubectl` with Kustomize support and, for local Secret generation,
OpenSSH tools. The commands below run from the repository root. The secret
generation block uses Bash (Git Bash or WSL on Windows); the remaining examples
use PowerShell. Replace `python` with your Python executable if necessary.

```powershell
python -m pip install -r deploy/k8s/cowrie-requirements.txt
```

Before deploying, the infrastructure owner must provide the EKS cluster, capacity,
EFS CSI driver/IAM, EFS filesystem and three access points, Metrics Server, S3
bucket, and uploader Pod Identity roles/associations. See
[`infra/cowrie/README.md`](../infra/cowrie/README.md) for networking, UID/GID,
permissions and IAM details. Verify the current cluster version and use a
compatible kubectl; the recovery described Kubernetes 1.37.

The backed-up Service uses `loadBalancerClass: eks.amazonaws.com/nlb` (EKS Auto
Mode), IP targets, internet-facing NLBs, TCP health checks and cross-zone load
balancing. These functional annotations are preserved. Another controller requires
a deliberate Service adaptation. Applying a LoadBalancer Service can create AWS
resources and incur charges. Restrict SSH to the approved source CIDRs before
applying it. Cowrie must remain isolated from FL/training services; the recovery
did not contain NetworkPolicies. Establish and verify network isolation for the
target cluster before exposing the honeypot.

## Configure and render locally

```powershell
New-Item -ItemType Directory -Force .local/cowrie | Out-Null
Copy-Item deploy/k8s/overlays/aws/cowrie/settings.example.json .local/cowrie/settings.json
```

Edit `.local/cowrie/settings.json` with the actual region, bucket, EFS filesystem,
three distinct access point IDs, one uploader role ARN per organisation and
approved IPv4 CIDRs. The renderer rejects unresolved placeholders, duplicate
access points, malformed IDs/ARNs and unrestricted `/0` source ranges. Do not put
AWS access keys, session tokens or passwords into settings. Authenticate local AWS
CLI sessions through your normal profile/SSO outside the repository.

```powershell
python deploy/k8s/cowrie_config.py validate
python deploy/k8s/cowrie_config.py render
python deploy/k8s/cowrie_config.py validate --rendered
```

The renderer only runs `kubectl kustomize`, validates references and writes five
files in `.local/cowrie/rendered/`: `namespaces.yaml`, `storage.yaml`,
`configuration.yaml`, `workloads.yaml` and `services.yaml`. It does not call AWS or
the Kubernetes API. It changes structured fields rather than substituting shell
variables in the upload script. Review those files before applying. Render again
after source/configuration changes; stale files are not updated automatically.

## Generate Secrets locally

`services/cowrie/secrets.example.yaml` shows the required names and keys using
plain placeholders only. **Do not apply it.** No real Secret data, including
base64 data, belongs in Git. Generate fresh keys separately for each organisation;
all replicas in that namespace then share its SSH identity. If a restored external
deployment must retain its old SSH fingerprint, recover its keys securely outside
Git instead of copying backup exports into this repository.

In Bash, generate local files only (the `kubectl create secret --dry-run=client`
commands below do not create cluster Secrets):

```bash
set -euo pipefail
umask 077
mkdir -p .local/cowrie/secrets
for org in orga orgb orgc; do
  dir=".local/cowrie/secrets/$org"
  mkdir -p "$dir"
  # Run once; do not overwrite an existing key set.
  for algorithm in rsa ecdsa ed25519; do
    if [ -e "$dir/ssh_host_${algorithm}_key" ]; then
      echo "Existing key set for $org; inspect it before regenerating" >&2
      exit 1
    fi
  done
  ssh-keygen -q -t rsa -b 3072 -N "" -f "$dir/ssh_host_rsa_key"
  ssh-keygen -q -t ecdsa -b 256 -N "" -f "$dir/ssh_host_ecdsa_key"
  ssh-keygen -q -t ed25519 -N "" -f "$dir/ssh_host_ed25519_key"
  cp services/cowrie/userdb.example.txt "$dir/userdb.txt"
  # Set organisation-specific DECOY login entries; never reuse a real password.
  "${EDITOR:-vi}" "$dir/userdb.txt"
  if grep -q REPLACE_ "$dir/userdb.txt"; then
    echo "Replace userdb placeholders before continuing" >&2
    exit 1
  fi
  kubectl -n "$org" create secret generic cowrie-host-keys \
    --from-file="$dir/ssh_host_rsa_key" --from-file="$dir/ssh_host_rsa_key.pub" \
    --from-file="$dir/ssh_host_ecdsa_key" --from-file="$dir/ssh_host_ecdsa_key.pub" \
    --from-file="$dir/ssh_host_ed25519_key" --from-file="$dir/ssh_host_ed25519_key.pub" \
    --dry-run=client -o yaml > ".local/cowrie/secrets/$org-host-keys.secret.yaml"
  kubectl -n "$org" create secret generic cowrie-userdb \
    --from-file="$dir/userdb.txt" --dry-run=client -o yaml \
    > ".local/cowrie/secrets/$org-userdb.secret.yaml"
done
```

Restrict access to `.local/` on your machine (Windows ACLs where applicable).
Do not paste the output of Secret commands into issues, PRs or reports. After
rotating Secrets/configuration, restart Cowrie pods deliberately: the file mounts
use `subPath`, so changes do not refresh inside existing pods automatically.

## Deployment order (manual, after review)

These commands are instructions for a future deployment; preparation and offline
validation do not execute them. Confirm `kubectl config current-context` first.
On an existing cluster, inspect resources with these names and coordinate with
their owners before applying. Do not overwrite another team's workload or rebind
a volume belonging to a live claim.

1. Complete the AWS prerequisites through the infrastructure owner's stack. The
   backup's AWS JSON files are describe-output reference snapshots, **not directly
   deployable infrastructure**, CloudFormation templates or create API inputs.
2. Apply namespaces, then storage:

   ```powershell
   kubectl apply --dry-run=server -f .local/cowrie/rendered/namespaces.yaml
   kubectl apply -f .local/cowrie/rendered/namespaces.yaml
   kubectl apply --dry-run=server -f .local/cowrie/rendered/storage.yaml
   kubectl apply -f .local/cowrie/rendered/storage.yaml
   kubectl get pv
   foreach ($org in @('orga','orgb','orgc')) { kubectl -n $org get pvc cowrie-logs }
   ```

   Wait for each PVC to be `Bound` to its matching `cowrie-logs-<org>` PV. Static
   binding deliberately retains `PVC.spec.volumeName` and a PV `claimRef` with
   name/namespace only. Old claim UID/resourceVersion, binding annotations,
   protection finalizers and status are removed. Explicit empty storage classes
   avoid accidental default dynamic provisioning. Each PV uses `Retain`, TLS and
   its organisation's access point. `5Gi` is a Kubernetes binding value, not an EFS
   capacity quota. Old retained PVs may still reference a deleted claim UID:
   reconcile those manually only after confirming the old claim is gone and the
   data/access point owner approves. New manifests cannot clear stale live state
   safely just by being reapplied.
3. Apply the locally generated Secrets and shared configuration:

   ```powershell
   Get-ChildItem .local/cowrie/secrets -Filter *.secret.yaml | ForEach-Object { kubectl apply -f $_.FullName }
   kubectl apply --dry-run=server -f .local/cowrie/rendered/configuration.yaml
   kubectl apply -f .local/cowrie/rendered/configuration.yaml
   ```

4. Verify the Pod Identity associations for `cowrie-log-uploader` in all three
   namespaces. The role ARNs in settings do not configure these associations.
   The infrastructure owner must create them externally, with matching trust and
   S3 prefix permissions. For an approved future CLI setup, use
   `aws eks create-pod-identity-association --cluster-name <cluster> --namespace <org> --service-account cowrie-log-uploader --role-arn <role-arn>`.
   Do not add AWS credentials to a ConfigMap/Secret or give Cowrie the uploader role.
5. Apply workloads and inspect pods/HPA before creating public exposure:

   ```powershell
   kubectl apply --dry-run=server -f .local/cowrie/rendered/workloads.yaml
   kubectl apply -f .local/cowrie/rendered/workloads.yaml
   foreach ($org in @('orga','orgb','orgc')) {
     kubectl -n $org rollout status deployment/cowrie
     kubectl -n $org get pods,hpa,cronjobs
   }
   ```

6. Confirm network isolation, approved CIDRs and the NLB configuration, then apply
   `services.yaml` using the same server-dry-run/apply sequence. This step can
   provision NLBs. Connect from an allowed source to the resulting hostname on
   TCP port `2222`; verify a disallowed source is blocked.

## Validation and uploader activation

The local validator checks YAML parsing, duplicate resource identities, namespace
and ConfigMap/Secret/PVC/service-account references, ConfigMap keys, volume mounts,
Deployment/Service selectors and target ports, HPA target/bounds/CPU and scaling
policies, CronJob scheduling and EFS claim reservations. It also checks UID-based
replica log mounts, non-destructive directory preparation, and that the uploader
mounts the same claim at its root without a pod-specific subpath. Kustomize rewrites generated
ConfigMap names with content hashes; the validator checks those final references.
It reports the two intentionally external Secrets in each namespace.

For API schema validation, download the official Kubernetes `swagger.json` for
your target release outside the repo and run:

```powershell
python deploy/k8s/cowrie_config.py validate --rendered --schema <path-to-swagger.json>
```

This performs offline structural schema checks, adapting Kubernetes OpenAPI's
`IntOrString` format to accept either integers or strings. API admission, IAM, actual EFS
ownership/mounting, container startup, metrics availability, NLB routing/source
filtering and S3 access require a live cluster. Verify the UID-based log
subdirectories are writable and separate in each access point. Inspect events,
`kubectl top pods`, HPA conditions and `kubectl describe` output without dumping
Secrets. Confirm Cowrie cannot access uploader credentials or training workloads.

The CronJob retains the backup's `suspend: true`, `Forbid` concurrency policy,
midnight `Asia/Singapore` schedule, one-hour start/execution deadlines, three
retries and history limits of three. Generate a controlled, non-sensitive Cowrie
log first. A **manual Job can run even while the CronJob is suspended**:

```powershell
kubectl -n orga create job --from=cronjob/cowrie-logs-to-s3 cowrie-upload-validation
kubectl -n orga wait --for=condition=complete job/cowrie-upload-validation --timeout=3600s
kubectl -n orga logs job/cowrie-upload-validation
```

Repeat for orgb/orgc and verify the STS identity and objects under
`s3://<bucket>/<org>/raw/<UTC-date>/`. Object names include upload timestamp,
uploader pod UID, source pod UID and original filename. The uploader skips symlinks
and `cowrie.json.pipeline-test*`, fails if there are no matching log files, copies
active and rotated logs, and does not delete or deduplicate source data. Active
files can change during upload; a successful copy is not a consistent archival
snapshot. Repeated runs create additional objects. Verify object contents and
cross-organisation denial with your operator identity; uploader roles intentionally
cannot read or delete logs. Do not approve EFS deletion based only on copy success.

After validation, deliberately change the source CronJob's `suspend` setting and
update its validator policy in a reviewed change, then render/apply again. For a
temporary live trial, `kubectl -n <org> patch cronjob cowrie-logs-to-s3 --type=merge -p '{"spec":{"suspend":false}}'`
enables scheduling, but reapplying current manifests suspends it again. Unsuspending
can immediately schedule a missed run within `startingDeadlineSeconds`; plan for it.

## HPA configuration and test status

| Configuration | Scale-down | Runtime test status |
| --- | --- | --- |
| Previously tested recovery baseline | `selectPolicy: Disabled`; scale-up only | Historical scale-up testing; not rerun during repository preparation |
| Current repository configuration | 300-second stabilization, `Min`, one pod per 60 seconds | **Untested on a live cluster**; local manifest/schema checks only |

All three organisation overlays inherit the same updated HPA. Min replicas `1`,
max replicas `3`, CPU utilisation target `60%`, scale-up `Max` of four pods or
100% per 15 seconds, and zero scale-up stabilization remain unchanged. The new
scale-down policy is:

```yaml
scaleDown:
  stabilizationWindowSeconds: 300
  selectPolicy: Min
  policies:
    - type: Pods
      value: 1
      periodSeconds: 60
```

With sustained low CPU demand, the HPA can reduce replicas toward one, using the
highest recommendation in the trailing 300-second stabilization window and
removing at most one replica per 60 seconds. Metric collection and reconciliation
can delay changes further; five minutes is not an exact wall-clock deadline.
The Deployment still starts at the backed-up three replicas. Reapplying its
`replicas: 3` can reset an HPA-managed count, so do not reapply the Deployment
during a scaling measurement.

**Scale-down can disconnect active SSH sessions. CPU-based HPA does not check
whether sessions are active.** A low-CPU interactive session does not prevent
its pod from being terminated. The existing 30-second termination grace period
does not guarantee that a session finishes or all buffered log events are flushed.
No session-draining mechanism is added by this policy.

### Persistent logs across replica deletion

The manifest wiring is verified locally for all three organisations:

- `POD_UID` comes from `metadata.uid` in both Cowrie and its init container.
  The init container creates `/efs/<pod-uid>` on the `cowrie-logs` PVC with
  `mkdir -p`; it does not remove existing directories.
- Cowrie mounts that same directory with `subPathExpr: $(POD_UID)` at
  `/cowrie/cowrie-git/var/log/cowrie`. Distinct pod UIDs give every replica,
  including replacement pods, a distinct persistent directory.
- The claim binds to its organisation's RWX EFS CSI PV/access point with `Retain`.
  Pod deletion does not delete the PVC or that pod's EFS directory. Within the
  access point, the layout is `<pod-uid>/cowrie.json*`.
- The uploader mounts the **whole same PVC** read-only at `/logs`, without
  `subPath` or `subPathExpr`. `upload.sh` scans `/logs/*/cowrie.json*` without
  consulting the Kubernetes pod inventory, so it includes directories belonging
  to deleted pods. Source logs are never removed by the uploader; the retained
  cleanup checker only reports candidates and has no automatic workload.

These checks confirm configuration, not actual EFS permissions, runtime writes
or S3 delivery. The local uploader test uses synthetic current/deleted-pod log
directories and a stub AWS command; it does not mount EFS or contact AWS.

### Manual scaling and retention test

Run this procedure only after an approved deployment to an isolated test
environment. Repeat for `orga`, `orgb` and `orgc`, saving timestamps, metrics,
replica counts, pod UIDs and upload evidence under ignored `.local/` storage.
Keep the upload CronJob suspended and use a manual Job for the final check.
Only use decoy SSH credentials and controlled, non-sensitive sessions/logs.

1. **Generate controlled CPU load and observe scale-up.** First let the idle HPA
   reach one replica; the manifest starts at three, so starting there cannot
   demonstrate scale-up. Confirm Metrics Server reports CPU and HPA conditions
   are healthy. In PowerShell, select the test namespace and an existing pod:

   ```powershell
   $org = 'orga' # Repeat the procedure for orgb and orgc.
   $pod = (kubectl -n $org get pods -l app=cowrie -o json | ConvertFrom-Json).items[0].metadata.name
   kubectl -n $org top pods -l app=cowrie
   # Foreground load, bounded to three minutes; observe from another terminal.
   kubectl -n $org exec $pod -c cowrie -- python3 -c "import time; end = time.monotonic() + 180; exec('while time.monotonic() < end: pass')"
   ```

   In a second terminal, run `kubectl -n <org> get hpa cowrie-hpa --watch` and
   `kubectl -n <org> get pods -l app=cowrie --watch`; sample `kubectl top pods`
   and record the rise toward three replicas. Verify `python3` exists in the
   chosen Cowrie image before the test; do not install load tools into live pods.
   Generate a benign SSH interaction against **each** replica, for example with
   an operator port-forward to each pod's port 2222. Confirm each pod has a
   nonempty `cowrie.json` in its mounted log directory. At the high replica count,
   save the pod/UID inventory before scale-down:

   ```powershell
   New-Item -ItemType Directory -Force .local/cowrie/scaling-test | Out-Null
   kubectl -n $org get pods -l app=cowrie -o json | Set-Content -Encoding utf8 ".local/cowrie/scaling-test/$org-before.json"
   ```

2. **Stop load and observe CPU usage drop.** The bounded load exits after 180
   seconds; confirm it has finished, end other test traffic and sample
   `kubectl -n <org> top pods -l app=cowrie` until CPU stays below the target.
   CPU utilisation here is relative to the configured `100m` CPU request.
   Record when low utilisation starts; do not infer session inactivity from it.

3. **Allow stabilization and observe scale-down.** Keep watching for at least
   the 300-second stabilization window after high recommendations age out, plus
   metric/controller delay. Expect reductions such as `3 -> 2 -> 1`, with no
   more than one replica removed per 60 seconds. Save HPA descriptions and events
   (`kubectl -n <org> describe hpa cowrie-hpa` and
   `kubectl -n <org> get events --sort-by=.lastTimestamp`) to assess actual timing.
   If replicas do not shrink, inspect CPU, metric errors, HPA conditions and
   recent recommendations rather than assuming failure at exactly five minutes.
   Record which pod UIDs disappeared. An optional low-CPU SSH session may be
   disconnected if its pod is selected; HPA makes no promise about which active
   sessions survive.

4. **Verify retained EFS logs and subsequent S3 upload.** After removed pods have
   terminated, capture the surviving pod UIDs and compare with the saved list:

   ```powershell
   $before = Get-Content -Raw ".local/cowrie/scaling-test/$org-before.json" | ConvertFrom-Json
   $after = kubectl -n $org get pods -l app=cowrie -o json | ConvertFrom-Json
   $removed = @($before.items.metadata.uid | Where-Object { $_ -notin $after.items.metadata.uid })
   $removed
   $job = 'cowrie-retention-' + (Get-Date -Format 'yyyyMMddHHmmss')
   kubectl -n $org create job --from=cronjob/cowrie-logs-to-s3 $job
   kubectl -n $org wait --for=condition=complete "job/$job" --timeout=3600s
   kubectl -n $org logs "job/$job"
   ```

   Require at least one removed UID and an `Uploaded:` entry for that UID's real
   `cowrie.json` file. A successful manual Job executed after deletion confirms
   its uploader could still read that directory through EFS. With an operator
   identity, list the expected S3 prefix, confirm object names contain the removed
   UID, and inspect the uploaded benign events/content; uploader roles cannot
   read objects. Check surviving pods' logs too. Preserve the before/after
   inventory, upload output and object evidence locally; a Job success alone
   could mean it uploaded only surviving pods' logs. Remove the test Job after
   reviewing evidence, leaving the EFS source directories and CronJob suspension
   intact. Do not treat retained files as proof that termination lost no buffered
   events; compare the expected test events separately.

## Cleanup

Suspend upload scheduling first, remove SSH Services to stop exposure, and wait
for their NLBs/target groups to be removed. Stop/finish manual upload Jobs, verify
required logs exist in S3, then delete workloads and local Secret objects. Commands
below assume these namespaces belong solely to this setup; inspect ownership first.

```powershell
foreach ($org in @('orga','orgb','orgc')) {
  kubectl -n $org patch cronjob cowrie-logs-to-s3 --type=merge -p '{"spec":{"suspend":true}}'
}
kubectl delete -f .local/cowrie/rendered/services.yaml
kubectl delete -f .local/cowrie/rendered/workloads.yaml
# Remove any manual validation Jobs separately after reviewing their results.
kubectl delete -f .local/cowrie/rendered/configuration.yaml
Get-ChildItem .local/cowrie/secrets -Filter *.secret.yaml | ForEach-Object { kubectl delete -f $_.FullName }
```

Only after pods/jobs have released claims, delete PVCs and then PV objects from
`storage.yaml` if retiring this installation. `Retain` keeps EFS data; deleting PVs,
PVCs or namespaces does not erase EFS content or the S3 archive. Namespace deletion
also deletes unrelated resources inside it, so remove namespaces only after an
ownership review. Retire Pod Identity associations/IAM and any dedicated NLB/network
resources through the infrastructure owner. EFS access points, filesystems, bucket
objects and KMS keys need separate deliberate retention/deletion decisions.

Keep local Secret/key material protected or securely dispose of it after retirement.
Keep original recovery backups intact. `.gitignore` excludes `.local/`, backup
exports, credentials, PEM/private keys and generated Secret files, but does not
protect files already tracked or force-added. Before publishing, review
`git diff --check`, `git diff`, `git ls-files --others --exclude-standard` and run
a sensitive-content scan that includes new files. Never stage raw recovery JSON.

References: [Kubernetes HPA behavior](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/),
[EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html),
[EFS prerequisites](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html),
[EKS NLB annotations](https://docs.aws.amazon.com/eks/latest/userguide/auto-configure-nlb.html).
