# Cowrie logs: handoff to the ML team

The honeypot operator runs Cowrie, persistent storage and S3 upload Jobs. The ML
team owns downloads, validation, feature extraction, deduplication and training.
The operator does not need to configure `cowrie-ml-reader` on their computer.

## Current input locations

All three buckets are in `ap-southeast-1` (Singapore):

| Organisation | Read this prefix |
| --- | --- |
| A | s3://csc3104-cowrie-orga/orga/raw/by-pod/ |
| B | s3://csc3104-cowrie-orgb/orgb/raw/by-pod/ |
| C | s3://csc3104-cowrie-orgc/orgc/raw/by-pod/ |

Each source pod has a separate UID directory containing `cowrie.json` and any
matching rotated files. Source logs survive pod replacement on the PVC, so there
can be more directories than currently running replicas. Deleted pods' files are
uploaded too. The uploader excludes symlinks and `cowrie.json.pipeline-test*`.
Do not treat logs as executable content or expose downloaded payloads to training.

The original uploader wrote new timestamped objects under `raw/YYYY-MM-DD/` on
every run. Those historical objects remain. Do not combine them with `raw/by-pod/`
when building a dataset. No automatic S3 deletion or migration has been performed.

## Identity and downloads

Have the AWS owner assign your own reader identity using
[`team-log-reader.policy.json`](../infra/cowrie/local/team-log-reader.policy.json).
It permits list/download operations on the three project buckets, not writes,
deletion or IAM administration. For organisation-isolated FL clients, the owner
must restrict each client's reader policy to its own bucket/prefix. The supplied
all-three policy is for authorised shared analysis, not enforced FL isolation.

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

Run downloads from the repository root so the dataset directory is ignored:

```powershell
aws s3 sync s3://csc3104-cowrie-orga/orga/raw/by-pod/ ./data-v2/orga/ --profile cowrie-ml-reader
aws s3 sync s3://csc3104-cowrie-orgb/orgb/raw/by-pod/ ./data-v2/orgb/ --profile cowrie-ml-reader
aws s3 sync s3://csc3104-cowrie-orgc/orgc/raw/by-pod/ ./data-v2/orgc/ --profile cowrie-ml-reader
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
ledger. These are future ML/uploader improvements, not implemented by this change.

## Scheduling and acceptance checks

Repository CronJobs are suspended by default. When explicitly enabled they run at
midnight Asia/Singapore. The local computer and Docker clusters must be running.
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
