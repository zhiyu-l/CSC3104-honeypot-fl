# Combined nightly processor on EKS

Use this guide for AWS. [cowrie-nightly.md](cowrie-nightly.md) describes the
separate local kind lab; do not apply its credential Secrets or root init
container to EKS. Commands below are **PowerShell on the laptop**, from the
repository root, unless explicitly labelled Bash/CloudShell. EKS keeps running
when the laptop, Docker Desktop or CloudShell is closed.

## Recorded destination deployment

Operator-supplied results on 10 November 2026 (Singapore) established:

| Organisation | EKS cluster | Bucket | Uploader role |
| --- | --- | --- | --- |
| orga | orga-honeypot-cluster | csc3104-cowrie-orga-909891185696 | csc3104-orga-cowrie-uploader |
| orgb | orgb-honeypot-cluster | csc3104-cowrie-orgb-909891185696 | csc3104-orgb-cowrie-uploader |
| orgc | orgc-honeypot-cluster | csc3104-cowrie-orgc-909891185696 | csc3104-orgc-cowrie-uploader |

All use account `909891185696`, region `ap-southeast-1`, namespace `default`,
service account `cowrie-log-uploader`, PVC `cowrie-logs` and CronJob
`cowrie-logs-to-s3`. The deployed image was:

```text
909891185696.dkr.ecr.ap-southeast-1.amazonaws.com/cowrie-nightly@sha256:78ce6a0f621c5dfc1c8dec4b73af5f40beaa71ffe6428280540bc6c6dca9bc35
```

Manual combined Jobs succeeded in all three clusters. Each initial batch had
1,000 sessions and its downloaded CSV matched its manifest checksum. Subsequent
Jobs reported checkpoints containing 3,000 sessions per organisation. Finally,
all three schedules were enabled (`suspend: false`, midnight Asia/Singapore).
These are conversation-supplied observations, not a new live inspection.
Timer-triggered execution, backlog completion, cross-batch uniqueness, reader
identity access, multipart KMS uploads, HPA and network isolation remain separate
acceptance checks. The checkpoint count alone does not prove unique source data.

## Settings and infrastructure

Existing clusters need working EFS CSI/mount access, worker capacity, ECR pull
permissions on the worker identity, and Pod Identity support/association. The
processor uses the SDK default credential chain; no static AWS Secret is used.
The non-root init container creates `.nightly-state` on the organisation's EFS
root. Source logs mount read-only at `/logs`; only the state subdirectory mounts
writable at `/state`. UID/GID must match the storage permissions. Do not reset
state or change its organisation/bucket binding to work around an error.

Copy `deploy/k8s/overlays/aws/cowrie/settings.example.json` into ignored
`.local/cowrie/settings.json` only if you do not already have settings. Complete
every placeholder. Old settings with a top-level `s3_bucket` are rejected: move
the bucket into each organisation. Configure:

- Shared `aws_region`, `efs_filesystem_id` and digest-pinned `nightly_image`.
- Per-organisation cluster/context/ARN, EFS access point, source CIDRs, uploader
  role, `s3_bucket`, `uploader_uid` and `uploader_gid` (defaults 999).
- `kms_key_arn`: exact key ARN for SSE-KMS, or `null` for SSE-S3. Never leave it
  null for the destination KMS-encrypted buckets.
- `schedule_enabled`: false for a fresh install. Set true explicitly after
  verification; retain true when rendering an already enabled deployment.

Destination key IDs observed in the role policies were:

| Organisation | KMS key ID (account/region above) |
| --- | --- |
| orga | bcc9ab95-68f4-4cee-8793-44649e416b35 |
| orgb | 79672a44-fa31-45d8-8428-d0ff9072bc81 |
| orgc | 883e0734-6ab6-4326-ada8-5373c357543d |

Use ARN form `arn:aws:kms:ap-southeast-1:909891185696:key/<key-id>` after verifying
the bucket/key configuration. EFS IDs and source CIDRs were not established in
the conversation; obtain them from the actual infrastructure, not guesses.

## Build and publish

Sign into the destination profile locally (`aws login --profile cowrie-destination`),
select `$env:AWS_PROFILE = 'cowrie-destination'`, and check
`aws sts get-caller-identity`. CloudShell credentials do not sign in the laptop.
Docker Desktop must be running Linux containers for the build only.

```powershell
$region = 'ap-southeast-1'
$registry = '909891185696.dkr.ecr.ap-southeast-1.amazonaws.com'
$image = "${registry}/cowrie-nightly:eks-v2"
aws ecr describe-repositories --region $region --repository-names cowrie-nightly
# Only create the repository if the preceding error is RepositoryNotFoundException.
# aws ecr create-repository --region $region --repository-name cowrie-nightly
aws ecr get-login-password --region $region |
    docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw 'ECR login failed; do not continue' }
docker buildx build --platform linux/amd64,linux/arm64 -f services/log-processor/nightly.Dockerfile -t $image --push .
if ($LASTEXITCODE -ne 0) { throw 'Image build/push failed' }
aws ecr describe-images --region $region --repository-name cowrie-nightly --image-ids imageTag=eks-v2 --query 'imageDetails[0].imageDigest' --output text
```

Use the returned digest in settings, not the mutable tag. Existing verified
images can be reused when processor code has not changed. A Windows credential
helper failure is a local Docker issue, not a reason to recreate ECR or change
IAM. The deployment session used a temporary Docker config with
`cliPluginsExtraDirs` pointing to Docker Desktop's `resources/cli-plugins` and
file-based ECR auth, deleted in `finally`. Do not commit token-bearing configs;
omitting that plugin directory can hide Buildx. Normal credential storage is
preferred when working.

## Render and reconcile an existing installation

```powershell
python -m pip install -r deploy/k8s/cowrie-requirements.txt
python deploy/k8s/cowrie_config.py render
python deploy/k8s/cowrie_config.py validate --rendered
```

Review each `.local/cowrie/rendered/<org>/infra/uploader-policy.json`. It grants
raw PutObject/AbortMultipartUpload and processed-session PutObject, plus
GenerateDataKey/Decrypt on the selected KMS key when configured. Install through
the infrastructure owner's existing IAM management path; inspect other inline
and managed policies before replacing a policy. Key/bucket policies can still
deny requests. This does not create a role or duplicate existing associations.

`reader-policy.json` is generated per organisation with prefix-limited ListBucket,
GetObject and optional KMS Decrypt. Attach to the intended reader identity or
permission set, not the upload-only role. It grants no writes and no automatic
cross-organisation access. These policies cover nightly sessions, not the
separate manual replay workflow. Test downloads using that reader's credentials.

For existing clusters, apply **only `nightly.yaml`** to update the ConfigMap,
service account and CronJob. It does not reconcile Cowrie, HPA, EFS PV/PVC or NLB.
The full AWS Service templates assume EKS Auto Mode; do not replace an existing
Load Balancer Controller Service with them without reviewing its controller.

```powershell
$org = 'orga' # Repeat separately for orgb and orgc.
$tool = 'deploy/k8s/cowrie_config.py'
$manifest = ".local/cowrie/rendered/$org/nightly.yaml"
python $tool check-context --org $org
python $tool kubectl --org $org -- apply --dry-run=server -f $manifest
if ($LASTEXITCODE -ne 0) { throw 'Server validation failed' }
python $tool kubectl --org $org -- apply -f $manifest
```

Do not run overlapping old raw-only manual Jobs during migration. Existing Jobs
keep their old pod templates; updating a CronJob affects only future Jobs.
The previous CloudShell `~/cowrie-uploader/*-nightly.json` files remain recovery
references. Archive obsolete raw-only manifests. Generated target metadata now
fingerprints settings and refuses stale files after bucket/image/schedule changes.

## Test, verify and enable

```powershell
$job = 'cowrie-nightly-test-' + (Get-Date -Format 'yyyyMMddHHmmss')
python $tool kubectl --org $org -- create job $job --from=cronjob/cowrie-logs-to-s3
python $tool kubectl --org $org -- wait --for=condition=complete "job/$job" --timeout=300s
python $tool kubectl --org $org -- logs "job/$job" -c uploader
```

On timeout, inspect the Job/pods and `prepare-processor-state` container logs.
ImagePullBackOff concerns node image access; PermissionError during preparation
concerns EFS identity/ownership. Do not solve it by resetting checkpoints.

Expect raw backup followed by `Published ... sessions` or `No new completed
sessions`. Require CSV plus manifest and verify hash/schema/row count using
[the ML handoff](cowrie-ml-handoff.md). Repeat manually to check checkpoints:
a backlog or new traffic can legitimately publish another batch. Each run selects
at most 1,000 completed sessions; active sessions wait. Nightly features are not
automatically labelled replay ground truth. Persistent locking also prevents
overlapping manual processors; CronJob Forbid alone covers scheduled Jobs only.

After validation, set that organisation's `schedule_enabled` to true in local
settings, render again and apply `nightly.yaml` with the same commands. Confirm
`get cronjob cowrie-logs-to-s3` shows `SUSPEND=False`. Do not merely patch live
state and leave settings false: the next apply would pause it again. Enabling
within the one-hour starting deadline may trigger a missed run.

After midnight, inspect `lastScheduleTime`, Jobs and logs in each cluster; verify
data freshness. To pause, set `schedule_enabled` false, render and apply the
focused file. Pausing does not stop active Jobs. Never delete the PVC or state
directory as routine cleanup. Completed test Jobs and unused raw-script ConfigMaps
may be removed only after checking references and preserving required evidence.

The files in `docs/cowrie-handoff-audit.md` and `docs/cowrie-local-audit.md` remain
historical local-lab records. This document records the newer AWS evidence without
claiming that manual tests verified every operational failure mode.

References: [ECR publishing](https://docs.aws.amazon.com/AmazonECR/latest/userguide/getting-started-cli.html),
[Pod Identity SDK support](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-minimum-sdk.html),
[KMS multipart permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateMultipartUpload.html),
[CronJob scheduling](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).
