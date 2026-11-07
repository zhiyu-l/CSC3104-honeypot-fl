# Combined nightly backup and feature processing

The three **local kind** overlays now configure the existing `cowrie-logs-to-s3`
CronJob to do both raw backup and feature processing at midnight Asia/Singapore.
They remain suspended in source until you test and explicitly resume them.
These changes do not update live IAM policies or deploy themselves.

## What each run does

1. Acquire an exclusive lock on persistent processing state.
2. Back up each pod's raw `cowrie.json*` files to `<org>/raw/by-pod/`.
3. Retry any pending feature publication using its original UUID and exact bytes.
   Otherwise find up to 1,000 new sessions with both connect and close events.
4. Use the shared Kaggle extractor to generate CSV features. Persist the batch
   on the PVC before attempting S3 publication.
5. Upload CSV and then its completion manifest to
   `<org>/processed/sessions/<batch-uuid>/`.
6. Atomically update the persistent checkpoint and remove the pending local batch.

All completed sessions are eligible, including historical smoke tests on the
first run. This is general session data, **not a verified replay test set**.
The ML team must select known replay sessions before evaluating a model. The
manual [selected replay workflow](cowrie-feature-processing.md) remains available
for explicit replay selections, using its separate `processed/replay/` prefix.

Cowrie continues logging during the run. Open sessions wait until a later run.
If there are no new completed sessions, only raw backup occurs. If a pending
batch is recovered, newly discovered work waits until the next run. Backlogs over
1,000 sessions take multiple runs. Incomplete sessions whose connect events have
already been lost cannot be reconstructed by this processor.

## 1. Update IAM permissions

The nightly output uses `processed/sessions/*` (the earlier manual processor uses
`processed/replay/*`). Update each dedicated uploader's live policy from the latest
`infra/cowrie/local/uploader-<org>.policy.json`. It includes both prefixes. If you
applied the earlier files already, apply the latest revision again. Use the updated
`team-log-reader.policy.json` for authorised all-three readers, or restrict a
reader to their organisation. No new keys or Secrets are required.

## 2. Build and load the image

From the repository root in PowerShell:

```powershell
docker build -f services/log-processor/nightly.Dockerfile -t cowrie-nightly:local-v1 .
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kind load docker-image cowrie-nightly:local-v1 --name "$org-honeypot-cluster"
    if ($LASTEXITCODE -ne 0) { throw "Image load failed: $org" }
}
```

## 3. Deploy while paused

Verify each `deploy/k8s/overlays/local/<org>/upload.patch.yaml` has `suspend: true`.
Then apply the local overlays (this also reconciles the existing Cowrie resources):

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kubectl --context "kind-$org-honeypot-cluster" -n default apply -k "deploy/k8s/overlays/local/$org"
    if ($LASTEXITCODE -ne 0) { throw "Apply failed: $org" }
    kubectl --context "kind-$org-honeypot-cluster" -n default get cronjob cowrie-logs-to-s3
}
```

The init container creates `.nightly-state/` on the existing `cowrie-logs` PVC.
The application runs as UID 999, mounts source logs read-only, and mounts only
the state subdirectory writable. No new bucket or PVC is needed. This is intended
for the existing single-node kind topology, not EKS or multi-node local storage.

## 4. Trigger and inspect a manual test

The existing operations helper now starts the combined workflow because it
creates its Job from the updated CronJob:

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy/k8s/overlays/local/cowrie-operations.ps1 -Action upload -Org $org
    if ($LASTEXITCODE -ne 0) { throw "Inspect the failed Job for $org" }
}
```

Expect `Backed up ... raw files`, followed by `Published ... sessions` or
`No new completed sessions`. Inspect each bucket's `<org>/processed/sessions/`
folder using the console or a reader identity; uploader identities cannot list
or download it. Verify the CSV SHA-256 against `csv_sha256` in its manifest.
Run the same block again: with no new completed sessions, it should publish no
new feature objects. Raw files are backed up again as intended. A large backlog
can legitimately generate another batch on the second run.

For failures, use `kubectl --context kind-orga-honeypot-cluster -n default get jobs,pods`
and inspect the named pod with `describe pod` and `logs <pod-name> -c uploader`.
The state preparation container is `prepare-processor-state`. Image errors mean
the nightly image needs loading. AccessDenied can mean the nightly prefix is
missing from the live IAM policy. A corrupt/partial JSON log or a processing
limit failure stops feature publication; it does not silently discard sessions.
Raw uploads may already have succeeded. Retry after resolving the cause.

## 5. Enable midnight scheduling after verification

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy/k8s/overlays/local/cowrie-operations.ps1 -Action resume -Org $org
    if ($LASTEXITCODE -ne 0) { throw "Resume failed: $org" }
}
```

Use `-Action pause` to suspend future schedules. Suspension does not stop an
already-running Job. There are still three CronJobs total, not six. The saved
patch state is updated by the operations helper so later applies preserve it.

## Recovery and limits

`checkpoint.json`, pending outputs and the lock live in `.nightly-state/` on
the log PVC. **Do not delete or reset this directory**: losing the checkpoint can
cause sessions to be published again under new batch IDs. PVC/cluster deletion
is not protected against; keep an appropriate backup of state with the logs.
Changing the bucket or organisation with existing state is rejected. Schema
upgrades do not reprocess already-published sessions automatically.

CronJob `concurrencyPolicy: Forbid` prevents overlapping scheduled Jobs; the
filesystem lock also excludes concurrent manually started Jobs. Failed Jobs
have bounded retries and a one-hour deadline. A process that loses its lock
exits rather than waiting indefinitely.

Feature publication retries **overwrite the same keys with the same persisted
bytes**. This differs from the manual replay uploader's conditional writes. It
handles a lost S3 acknowledgement or a crash between publication and checkpoint
commit without creating a second batch. Versioned buckets can retain identical
object versions on retries. It is not a guarantee against checkpoint loss,
external modification or storage corruption. Consumers require the completion
manifest and matching CSV hash, and should track session identifiers across
imports. Do not mix manual replay outputs and nightly outputs without deduplication.

Raw files are copied, never deleted. A raw backup is not a transaction across all
files while Cowrie writes/rotates them. Processing fails on inconsistent inputs
rather than publishing partial features. Feature overlap removal uses the existing
full-event heuristic described in the manual guide; it is not a universal event ID.

The first implementation scans retained logs each night and holds the processed
session ID list in memory. It is suitable for this local lab, not unbounded log
retention. The existing per-file/event limits apply, with at most 100,000 candidate
session IDs scanned and 1,000 selected per batch. Archive/retention and a database
checkpoint are future requirements for larger deployments. No automatic raw
deletion or S3 cleanup has been added.
