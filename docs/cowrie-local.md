# Cowrie on three local kind clusters

This guide reproduces the local development setup, not the EKS infrastructure.
Run PowerShell commands from the repository root. Requirements: Docker Desktop
running Linux containers, kind, kubectl, OpenSSH (`ssh-keygen`), and AWS CLI v2
for optional S3 uploads. Python on Windows is not required.

| Organisation | Explicit context | Namespace | SSH port | S3 bucket |
| --- | --- | --- | --- | --- |
| orga | kind-orga-honeypot-cluster | default | 2222 | csc3104-cowrie-orga |
| orgb | kind-orgb-honeypot-cluster | default | 2223 | csc3104-cowrie-orgb |
| orgc | kind-orgc-honeypot-cluster | default | 2224 | csc3104-cowrie-orgc |

Each cluster has one node, a Cowrie Deployment, HPA, ClusterIP SSH Service,
`cowrie-logs` PVC, configuration and a suspended upload CronJob. The shared
`local/cowrie` directory is a base for the organisation overlays; do not deploy
it independently. The old `docker-desktop/cowrie-local` deployment is a separate
smoke-test environment and is not required here. AWS uses its separate guide and
context-checking wrapper; never point the local commands at EKS.

## 1. Create clusters

```powershell
kind get clusters
```

For each cluster not already listed, run the corresponding command:

```powershell
kind create cluster --name orga-honeypot-cluster --wait 180s
kind create cluster --name orgb-honeypot-cluster --wait 180s
kind create cluster --name orgc-honeypot-cluster --wait 180s
```

Use explicit contexts in every operation; kind may change your current context.
Check each node is Ready and a `standard` storage class exists:

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kubectl --context "kind-$org-honeypot-cluster" get nodes
    kubectl --context "kind-$org-honeypot-cluster" get storageclass
}
```

## 2. Install Metrics Server

The original interactive setup used the latest release at installation time.
For a repeatable fresh installation, this example pins v0.8.0 (Kubernetes 1.31+).
Do not downgrade an existing working Metrics Server just to follow this example.
Review compatibility before changing the version.

```powershell
New-Item -ItemType Directory -Force .local/cowrie/metrics-server | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.0/components.yaml' -OutFile .local/cowrie/metrics-server/components.yaml
'[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' | Set-Content -Encoding ascii .local/cowrie/metrics-server/tls.patch.json
foreach ($org in @('orga', 'orgb', 'orgc')) {
    $context = "kind-$org-honeypot-cluster"
    kubectl --context $context apply -f .local/cowrie/metrics-server/components.yaml
    if ($LASTEXITCODE -ne 0) { throw 'Metrics Server apply failed.' }
    kubectl --context $context -n kube-system patch deployment metrics-server --type=json --patch-file .local/cowrie/metrics-server/tls.patch.json
    if ($LASTEXITCODE -ne 0) { throw 'Metrics Server patch failed.' }
    kubectl --context $context -n kube-system rollout status deployment/metrics-server --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw 'Metrics Server rollout failed.' }
}
```

The TLS exception is for local kind kubelets only; do not copy it to AWS.
Metrics can take another minute to appear in `kubectl top pods`.

## 3. Create Cowrie Secrets separately in each cluster

Choose one organisation and repeat this section for `orgb` and `orgc`:

```powershell
$org = 'orga'
$context = "kind-$org-honeypot-cluster"
$dir = ".local/cowrie/$org"
New-Item -ItemType Directory -Force "$dir/keys" | Out-Null
foreach ($algorithm in @('rsa', 'ecdsa', 'ed25519')) {
    $key = "$dir/keys/ssh_host_${algorithm}_key"
    if (-not (Test-Path $key)) {
        ssh-keygen -t $algorithm -f $key
        if ($LASTEXITCODE -ne 0) { throw 'Key generation failed.' }
    }
    if (-not (Test-Path "$key.pub")) { throw "Missing public key: $key.pub" }
}
if (-not (Test-Path "$dir/userdb.txt")) {
    "labuser:x:$org-decoy-only" | Set-Content -Encoding ascii "$dir/userdb.txt"
}
$keys = kubectl --context $context -n default create secret generic cowrie-host-keys "--from-file=$dir/keys" --dry-run=client -o json
if ($LASTEXITCODE -ne 0) { throw 'Host key Secret generation failed.' }
$keys | kubectl --context $context -n default apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Host key Secret apply failed.' }
$users = kubectl --context $context -n default create secret generic cowrie-userdb "--from-file=userdb.txt=$dir/userdb.txt" --dry-run=client -o json
if ($LASTEXITCODE -ne 0) { throw 'Login Secret generation failed.' }
$users | kubectl --context $context -n default apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Login Secret apply failed.' }
$keys = $users = $null
```

At each ssh-keygen passphrase prompt, press Enter twice. Preserve existing keys.
The sample passwords are decoys for localhost testing, not real account passwords.
Teammates generate their own keys; do not share the original operator's keys.
If replacing mounted keys or login data on an existing deployment, deliberately
restart Cowrie: its subPath Secret mounts do not refresh in running containers.

## 4. Deploy and connect

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    $context = "kind-$org-honeypot-cluster"
    kubectl --context $context -n default apply -k "deploy/k8s/overlays/local/$org"
    if ($LASTEXITCODE -ne 0) { throw "Apply failed for $org." }
    kubectl --context $context -n default rollout status deployment/cowrie --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "Rollout failed for $org." }
    kubectl --context $context -n default get 'pods,pvc,hpa,cronjobs'
}
```

Cowrie initially requests three replicas. HPA controls 1-3 replicas at a 60% CPU
request target, with 300 seconds of scale-down stabilization and at most one pod
removed per minute. `ScaleDownStabilized` with valid metrics is normal. Applying
the Deployment again can reset its replica count to three; do not reapply during
a scaling measurement. Scale-down can disconnect SSH sessions.

Run each forwarding command in its own terminal and leave it open:

```powershell
kubectl --context kind-orga-honeypot-cluster -n default port-forward --address 127.0.0.1 service/cowrie-ssh 2222:2222
kubectl --context kind-orgb-honeypot-cluster -n default port-forward --address 127.0.0.1 service/cowrie-ssh 2223:2222
kubectl --context kind-orgc-honeypot-cluster -n default port-forward --address 127.0.0.1 service/cowrie-ssh 2224:2222
```

From another terminal at the repository root:

```powershell
ssh -o UserKnownHostsFile=.local/cowrie/orga-known-hosts -p 2222 labuser@127.0.0.1
ssh -o UserKnownHostsFile=.local/cowrie/orgb-known-hosts -p 2223 labuser@127.0.0.1
ssh -o UserKnownHostsFile=.local/cowrie/orgc-known-hosts -p 2224 labuser@127.0.0.1
```

Use the matching decoy password, run `hostname`, `whoami`, `ls`, then `exit`.
Each port-forward selects one pod, not all replicas. Restart forwarding if its
selected pod is removed. Stop old port-forwards/standalone containers on port 2222.

## 5. S3 uploads (optional)

See [local AWS inputs and policies](../infra/cowrie/local/README.md).
The overlays record the team's existing Singapore buckets; change each overlay's
`S3_BUCKET` and `AWS_DEFAULT_REGION` if using different buckets. Cloning this repo
does not grant AWS access or create buckets, identities, or credentials.
Cowrie can run without AWS credentials while the CronJobs remain suspended.

Configure your own authorised profiles, never someone else's keys:

```powershell
aws configure --profile cowrie-orga
aws configure --profile cowrie-orgb
aws configure --profile cowrie-orgc
```

For SSO use `aws configure sso` and `aws sso login` instead. Profile snapshots of
temporary credentials expire; renew the login and rerun `credentials` before
expiry. This helper does not implement automatic renewal.

```powershell
$tool = './deploy/k8s/overlays/local/cowrie-operations.ps1'
foreach ($org in @('orga', 'orgb', 'orgc')) {
    aws sts get-caller-identity --profile "cowrie-$org"
    if ($LASTEXITCODE -ne 0) { throw 'AWS authentication failed.' }
    & $tool -Action credentials -Org $org
    & $tool -Action upload -Org $org
}
```

If Windows blocks script execution, review the script and run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal
before invoking it. This applies only to the current PowerShell process; it does
not change machine policy. Organisation-enforced policy may take precedence.

`credentials` captures credentials in memory and sends a Secret directly to the
explicit cluster. Only uploader pods reference it. Neither credential values nor
Secret manifests are written into source files. Kubernetes Secrets are not a
substitute for protecting local Docker/Kubernetes administrator access.

Check the Job logs for SUCCESS and inspect/download JSON objects in the S3 console
using your own reader identity. Upload-only profiles cannot list/read S3 objects.
After all three destinations are verified, enable the daily midnight Singapore
schedule explicitly:

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) { & $tool -Action resume -Org $org }
```

Pause it again:

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) { & $tool -Action pause -Org $org }
```

These operations update `upload.patch.yaml` and patch only live `spec.suspend`;
they do not reapply the Cowrie Deployment. Do not JSON-merge-patch the full
Kustomize upload patch: its partial container array lacks the base image.
Commit `suspend: true` as the team default. Pausing does not stop Jobs already
running; manual upload Jobs can run while suspended. Do not launch overlapping
manual upload Jobs: CronJob `Forbid` only governs scheduled Jobs.

The uploader scans `/logs/<pod-uid>/cowrie.json*`, including deleted pods' folders,
skips symlinks/test files, and never deletes local logs. Destinations are
`s3://<bucket>/<org>/raw/by-pod/<source-pod-uid>/<filename>`.
Every run uploads all matching files to stable keys. Unchanged source file sets
do not create additional keys; growing active files replace their previous objects.
Version-enabled buckets retain older versions and incur storage for them.
Active files may change during upload; this is not an atomic or incremental export.
Rotation can overlap event content with previously downloaded active files, so
stable keys do not provide event-level deduplication. The old dated snapshots
remain untouched. ML consumers must use only `raw/by-pod/`, not the entire `raw/`
prefix. See [the ML handoff](cowrie-ml-handoff.md) for access and ingestion guidance.
Only JSON logs are uploaded, not downloaded payloads or all Cowrie runtime files.
The computer must be awake with Docker running. The configured one-hour starting
deadline is not a guarantee to recover every missed schedule after downtime.

## Storage, recovery and evidence

Each cluster has its own dynamically provisioned `standard` RWO PVC requesting
5 GiB. All replicas share it on that cluster's one node; each pod UID has a separate
directory. Local-path capacity is not necessarily an enforced disk quota. The
root init container creates/chowns only the new pod's folder so Cowrie can write
as UID 999. Local storage is not multi-node shared EFS.

To inspect a pod's actual JSON log (the Cowrie image has no `tail`):

```powershell
$context = 'kind-orga-honeypot-cluster'
$pods = kubectl --context $context -n default get pods -l app=cowrie -o json | ConvertFrom-Json
$readLog = "from pathlib import Path; from collections import deque; p=Path('/cowrie/cowrie-git/var/log/cowrie/cowrie.json'); print(''.join(deque(p.open(), maxlen=20)) if p.exists() else 'No JSON log yet')"
foreach ($pod in $pods.items) {
    Write-Host $pod.metadata.name
    kubectl --context $context -n default exec $pod.metadata.name -c cowrie -- /cowrie/cowrie-env/bin/python3 -c $readLog
}
```

For a recovery test, choose a pod with SSH activity, record its UID, delete just
that pod, and wait for the Deployment rollout. A manual upload must still report
the removed UID's log filename; verify that object's contents in S3. Surviving
pod mounts only expose their own folder, not deleted pods' folders.

For a bounded scaling test, first wait for one ready replica. Record HPA output
with `get hpa cowrie-hpa --watch` in another terminal, then:

```powershell
$pods = kubectl --context $context -n default get pods -l app=cowrie -o json | ConvertFrom-Json
$running = @($pods.items | Where-Object { $_.status.phase -eq 'Running' -and -not $_.metadata.deletionTimestamp })
if ($running.Count -ne 1) { throw 'Wait for one pod before starting.' }
$load = "import time; end=time.monotonic()+180; exec('while time.monotonic() < end: pass')"
kubectl --context $context -n default exec $running[0].metadata.name -c cowrie -- /cowrie/cowrie-env/bin/python3 -c $load
```

Observe CPU and replicas rise, then return to one after load stops and stabilization
expires. This is a CPU-controller test, not a measurement of SSH capacity.
Keep logs/evidence in ignored `.local/cowrie/`; do not commit attacker-controlled logs.

Reported manual checks on 2026-11-03: all three PVCs Bound, SSH activity present,
Organisation A pod replacement, and successful S3 uploads (A: six files; B/C:
five each) with the original dated layout. A's upload included the removed pod UID.
Subsequent operator output confirmed seven files each uploaded successfully to
B and C's new `raw/by-pod/` prefixes. A's new-layout upload and the unchanged-file
repeat-upload object-count check were instructed but their results were not supplied.
These counts describe those runs, not an expected replica/file count. Automated schedules and every organisation's
full scale-up/down retention cycle still require operator verification.

## Team handoff and cleanup

Commit manifests, scripts, policy documents and this guide. Do not commit `.local/`,
AWS credentials, kubeconfigs, SSH keys, Secret exports or logs. Teammates create
their own clusters and use their own AWS identities. IAM does not grant remote
access to someone else's local kind cluster. Use Identity Center/team roles for
people; keep machine uploader permissions separate from reader/admin permissions.
The honeypot operator does not need an ML reader profile or ML implementation.
Share [the handoff](cowrie-ml-handoff.md) with the teammate responsible for processing.

Pause uploads before cleanup. Deleting a pod retains its log folder, but deleting
the PVC, namespace or kind cluster can delete local data. Export needed evidence
first. Existing S3 objects are unaffected by removing the local cluster. Never
delete `default` as a cleanup shortcut. Revoke obsolete uploader keys separately.

## Offline repository checks

Maintainers need Python 3.10+ for these checks (normal local operation does not).
Run in a virtual environment with kubectl and a POSIX shell/Git Bash available:

```powershell
python -m pip install -r deploy/k8s/cowrie-requirements.txt
python -m unittest discover -s deploy/k8s/tests -v
```

These tests render both AWS and local overlays without cluster calls, verify
organisation boundaries, and run the uploader with a stub AWS command. The local
overlay test expects committed schedules to remain suspended. Test results and
remaining live checks are recorded in [the configuration audit](cowrie-local-audit.md).

References: [Metrics Server](https://github.com/kubernetes-sigs/metrics-server),
[kind](https://kind.sigs.k8s.io/docs/user/quick-start/),
[RWO semantics](https://kubernetes.io/docs/concepts/storage/persistent-volumes/),
[AWS profile export](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html).
