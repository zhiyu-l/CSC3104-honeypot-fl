# Cowrie logs: handoff to the ML team

## Start here: receive the current feature batches

The destination EKS deployment in account `909891185696` published an initial
1,000-session batch per organisation on 10 November 2026; operator downloads
passed CSV checksum checks. Subsequent checkpoints reported 3,000 sessions per
organisation and all three midnight Singapore schedules were enabled. Each run
is capped at 1,000; backlog completion and timer-triggered execution remain
unverified. See [the EKS runbook](cowrie-eks-nightly.md). The
[7 November audit](cowrie-handoff-audit.md) describes the separate local lab,
whose buckets lacked the account suffix and whose schedules were paused.

| Organisation | Current processed data |
| --- | --- |
| A | `s3://csc3104-cowrie-orga-909891185696/orga/processed/sessions/` |
| B | `s3://csc3104-cowrie-orgb-909891185696/orgb/processed/sessions/` |
| C | `s3://csc3104-cowrie-orgc-909891185696/orgc/processed/sessions/` |

Use your assigned reader identity, region `ap-southeast-1`, and the repository
revision supplied by the operator. A clone does not create your AWS profile.
Configure that profile as explained under **Identity and downloads**, then run
this from the repository root. The example assumes authorised all-three access;
download only organisations you are permitted to access:

```powershell
& {
    foreach ($org in @('orga', 'orgb', 'orgc')) {
        aws s3 sync "s3://csc3104-cowrie-${org}-909891185696/$org/processed/sessions/" "./data-v2/eks-909891185696/nightly/$org/" --profile cowrie-ml-reader
        if ($LASTEXITCODE -ne 0) { throw "Download failed for $org" }
    }
}
```

Verify completeness, hash and row count before passing the downloaded data to
the ML loader. This prints batch paths and counts, not source session data:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $csvFiles = @(Get-ChildItem ./data-v2/eks-909891185696/nightly -Recurse -File -Filter sessions.csv)
    if ($csvFiles.Count -eq 0) { throw 'No session CSVs downloaded' }
    foreach ($csvFile in $csvFiles) {
        $manifestPath = Join-Path $csvFile.DirectoryName 'manifest.json'
        if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Incomplete batch: $($csvFile.DirectoryName)" }
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        $actualHash = (Get-FileHash -LiteralPath $csvFile.FullName -Algorithm SHA256).Hash
        if ($actualHash -ine $manifest.csv_sha256) { throw "CSV checksum mismatch: $($csvFile.FullName)" }
        if ($manifest.schema_version -ne 2) { throw 'Unexpected schema version; check with the operator' }
        $rows = @(Import-Csv -LiteralPath $csvFile.FullName)
        if ($rows.Count -ne $manifest.rows) { throw "Row count mismatch: $($csvFile.FullName)" }
        Write-Host "Verified $($csvFile.DirectoryName): $($rows.Count) rows"
    }
}
```

This checks transfer integrity, not suitability for evaluation. Keep each CSV
with its manifest and record imported batch IDs, CSV hashes and session IDs.
Do not append the same batch to a dataset twice. Manifests without a CSV are
also incomplete and must not be ingested.

## Ownership and ML contract

| Owner | Responsibility |
| --- | --- |
| Honeypot operator | Clusters, PVCs, Cowrie, uploader identities, publication, checkpoints and operational recovery |
| Replay owner | Replay held-out attacks; map original dataset sessions to new `(organisation, pod UID, Cowrie session ID)` values and expected labels |
| ML teammate | Training split/features, reader setup, batch validation, import deduplication, replay selection, training and evaluation |

CSV metadata columns are `honeypot`, `session_id`, `src_ip`, `start`, `label`.
The remaining 16 raw numeric columns match `FEATURE_NAMES` in
`libs/fl-common/src/fl_common/schema.py` (schema version 2). `honeypot` is `orgA`,
`orgB` or `orgC`; output session IDs include organisation, pod UID and Cowrie
session ID. Both pipelines use `tools/cowrie_dataset/cowrie_sessions.py`.

Apply the ML loader's validation, clipping and log transform exactly once;
published CSV features are raw. Modeled labels are `brute_force`, `recon`,
`persistence`, `malware_download`; `scan` and `tunnel` remain visible but are
excluded by the current model. These are rule-based labels, not independent
proof that the intended attack succeeded.

Nightly batches contain all eligible completed sessions, including old tests.
Do not substitute them for the Kaggle training split or call them a held-out
evaluation set without verified replay mapping. Freeze training/evaluation
splits and use the same schema/extractor revision. The manifest records
`extractor_sha256`, `schema_version`, `feature_names` and source hashes for review.
A checksum proves integrity, not provenance.

`tools/evaluate_model.py` accepts this feature schema, but does not enforce the
S3 manifest contract or select replay membership for you. Complete those steps
before invoking it. Do not combine raw snapshots, manual replay outputs and
nightly batches as independent observations of the same sessions.

## Background and raw-log access

The combined [EKS nightly pipeline](cowrie-eks-nightly.md) publishes general completed
session features under `<org>/processed/sessions/<batch-uuid>/`. These include
ordinary SSH activity and historical smoke tests; they are not automatically a
held-out replay dataset. Identify replay sessions before evaluation. Require the
completion manifest and verify the CSV hash, just as for manual replay outputs.

The honeypot operator runs Cowrie, persistent storage, raw S3 uploads and the
[selected replay feature Job](cowrie-feature-processing.md). The ML team owns
Kaggle training-data preparation, downloads, validation, cross-run deduplication,
training and evaluation. Both processing paths use the shared session extractor.
The operator does not need to configure `cowrie-ml-reader` on their computer.

For processed replay evaluation, read `<org>/processed/replay/<run-id>/` in the
corresponding bucket. Require `manifest.json` and verify its CSV hash before
loading `sessions.csv`. These prefixes exist only after a successful feature
Job; the raw input locations below remain available for investigation/reprocessing.
See the feature guide for selection, label limitations and the publication contract.
The destination EKS deployment and generated IAM policies currently cover nightly
sessions only; the separate manual replay workflow has not been deployed there.

## Raw input locations (optional investigation/reprocessing)

All three buckets are in `ap-southeast-1` (Singapore):

| Organisation | Read this prefix |
| --- | --- |
| A | s3://csc3104-cowrie-orga-909891185696/orga/raw/by-pod/ |
| B | s3://csc3104-cowrie-orgb-909891185696/orgb/raw/by-pod/ |
| C | s3://csc3104-cowrie-orgc-909891185696/orgc/raw/by-pod/ |

Each source pod has a separate UID directory containing `cowrie.json` and any
matching rotated files. Source logs survive pod replacement on the PVC, so there
can be more directories than currently running replicas. Deleted pods' files are
uploaded too. The uploader excludes symlinks and `cowrie.json.pipeline-test*`.
Do not treat logs as executable content or expose downloaded payloads to training.

The older local uploader wrote timestamped objects under `raw/YYYY-MM-DD/` on
every run. Their presence in the new buckets is unverified. Do not combine them with `raw/by-pod/`
when building a dataset. No automatic S3 deletion or migration has been performed.

## Identity and downloads

Have the AWS owner assign your reader identity the per-organisation
`reader-policy.json` generated under `.local/cowrie/rendered/<org>/infra/` by
[the AWS renderer](../deploy/k8s/cowrie_config.py). It scopes list/download access
to that organisation and adds KMS Decrypt on its configured key. Key policies
must also permit access. Grant multiple organisations only to authorised shared
analysts; FL clients should receive only their own organisation's policy.
The old [local reader policy](../infra/cowrie/local/team-log-reader.policy.json)
targets the local lab, not the destination buckets. Operator downloads as WeiZhi
do not verify access for a teammate's reader identity.

Console users receive individual sign-in details; no access key is necessary for
browser downloads. An application needs API credentials, not a console password.
Prefer temporary credentials where supported. For the agreed local-lab IAM-key
method, create a dedicated reader identity, generate its access key outside Git,
and configure it on the ML developer's machine:

```powershell
aws configure --profile cowrie-ml-reader
aws sts get-caller-identity --profile cowrie-ml-reader
```

Enter region `ap-southeast-1` and output `json`. The profile name is local to that
computer; it is not created by cloning this repository or creating an IAM user.
If using Identity Center instead, use `aws configure sso` and `aws sso login` with
your assigned reader permission set. Never use or distribute the uploader keys.

For optional raw-log investigation, use these commands from the repository root.
For normal feature consumption use the processed-data commands above:

```powershell
aws s3 sync s3://csc3104-cowrie-orga-909891185696/orga/raw/by-pod/ ./data-v2/eks-909891185696/raw/orga/ --profile cowrie-ml-reader
aws s3 sync s3://csc3104-cowrie-orgb-909891185696/orgb/raw/by-pod/ ./data-v2/eks-909891185696/raw/orgb/ --profile cowrie-ml-reader
aws s3 sync s3://csc3104-cowrie-orgc-909891185696/orgc/raw/by-pod/ ./data-v2/eks-909891185696/raw/orgc/ --profile cowrie-ml-reader
```

Start with an empty dataset directory for this layout; don't merge an old download
of the dated snapshot layout. `sync` here does not delete local files that have
disappeared from S3. For a reproducible evaluation, record a manifest of downloaded
objects and content checksums and freeze the downloaded dataset for that run.

For containers, either download on the host and mount only the dataset, or inject
the application's own credentials at runtime through a secret mechanism. Host
profiles are not inherited automatically. The SDK can use AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY and AWS_DEFAULT_REGION; temporary credentials additionally
need AWS_SESSION_TOKEN and renewal. Do not bake credentials into images or source.

## Stable objects are not unique events

Current object key:

```text
<org>/raw/by-pod/<source-pod-uid>/<original-filename>
```

Unchanged source filenames upload to the same keys; changing active log contents
replaces those objects. New pods and new rotated files legitimately increase the
object count. Versioning, if enabled later, preserves previous versions. Copying
all files still incurs upload requests; this is not a change-only upload algorithm.

Important limitations:

- Active files can grow or rotate during copying; an uploaded file may end with
  an incomplete JSON line. Validate input and retry/quarantine incomplete records.
- Rotation can move events from the active log into a rotated file while a stale
  copy of the active file remains in S3 until it is overwritten. Thus even rebuilding
  from current keys may require deduplication during rotation.
- A future change to rotation that reuses filenames for different log segments
  can overwrite history. Verify rotation naming before treating this as an archive.
- Incrementally appending every downloaded file into training data repeats events.
  A file-level processing ledger alone is insufficient for growing active files.
- Deduplicating only on command, session or IP loses legitimate repeated activity.
  If no stable event ID exists, comparing canonical full event records (including
  timestamp/session plus organisation and source pod) is a heuristic; identical
  legitimate events may still collide. Document the limitations and keep raw data.

For stronger guarantees, introduce stable event IDs at ingestion or immutable,
completed log batches with reliable source offsets/segment IDs and a processing
ledger. The nightly processor now has a completed-session publication checkpoint,
but no universal event IDs or incremental raw-file offsets. The raw-data
limitations above still apply.

## Scheduling and acceptance checks

New installations default to suspended. The destination EKS schedules were enabled
for midnight Asia/Singapore; EKS does not depend on the laptop or CloudShell
staying open. Only kind requires the local computer and Docker clusters running.
ML consumers should check data freshness rather than assume a daily upload occurred.

Operator acceptance check for stable paths:

1. Upload and record object keys using a reader identity.
2. Upload again with the same source file set; keys should remain unchanged.
3. Generate new SSH events on an existing pod and upload again; its object should
   contain the new events at the same key.
4. Verify separate pods retain separate keys and a removed pod's file is present.

The supplied unit regression executes the uploader with a stub AWS CLI and checks
stable destinations, replacement contents, retained-pod logs and exclusions.
It does not prove live S3 permissions, rotation consistency or ML correctness.
