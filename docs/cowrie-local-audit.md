# Local configuration and documentation audit

Historical record. See the [7 November audit](cowrie-handoff-audit.md) for the
local combined nightly processor, or [the EKS runbook](cowrie-eks-nightly.md)
for the later destination-account deployment and current handoff boundaries.

Date: 3 November 2026 (Asia/Singapore).

This is a repository/offline audit, not a fresh inspection of AWS or live clusters.
No cluster apply, IAM change, bucket mutation, commit or push was performed during
this audit. The actual machine-specific credentials remain outside version control.

## Changes covered

- Three independent kind overlays, each in `default`, with organisation hostname,
  shared per-cluster RWO log claim and per-pod UID directories.
- HPA 1-3 replicas at 60% CPU; local storage permissions and readiness probe.
- Separate Singapore S3 buckets and uploader credential Secret references.
- Daily midnight Singapore CronJobs, saved with `suspend: true` for all three.
- PowerShell helper for status, credential snapshots, upload, pause and resume.
- Stable S3 keys `raw/by-pod/<pod-uid>/<filename>`, preserving source logs and
  previous pods' directories, and separating new inputs from dated snapshots.
- Scoped uploader policies, all-three-bucket reader policy, human/API access
  distinction, container credential handling and ML handoff.

## Corrections made during review

- Updated local, service and EKS guides that still described timestamped uploads.
- Removed the unused UPLOAD_POD_UID environment variable from the base CronJob.
  Existing Jobs/deployments are unaffected until the manifests are applied again.
- Clarified that the shared script change also affects future EKS render/apply;
  local evidence does not establish a running EKS deployment.
- Corrected claims about deduplication: stable object keys prevent new keys on
  repeat uploads, but active-file changes and rotation still require careful ML
  ingestion. No event-ID pipeline or incremental processor was implemented.
- Added `data-v2/` to `.gitignore`, matching the download commands already supplied.
- Documented paused defaults, temporary-credential expiry, failed full-container
  merge-patch behaviour, and the operator/ML team's separate responsibilities.
- Updated the old uploader regression and added local composed-overlay checks.

## Validation results

- All 14 tests in `deploy/k8s/tests` passed; none skipped. These include all three
  AWS overlay boundary checks, all three local Kustomize builds, ConfigMap reference
  resolution, local storage, identity labels, credentials limited to uploader pods,
  IAM bucket mapping, schedule defaults and service exposure.
- The uploader executed against a stub AWS CLI for each organisation: repeated
  runs used the same destination keys, appended events replaced the captured
  contents, deleted-pod logs remained included, synthetic test logs were excluded,
  and source files were preserved.
- PowerShell helper and all PowerShell examples in the local and ML guides parsed
  successfully. Pause/resume helper behaviour was mock-tested during its creation;
  this audit did not exercise live mutations or upload credentials.
- Relative Markdown links in changed/new files resolved. `git diff --check` passed.
- Changed/new files had no matches for AWS access-key IDs or private-key headers.
  This pattern check is not an exhaustive secret detector. Ignore checks confirmed
  local Secret files, datasets, AWS credentials and SSH host keys are excluded.

## Evidence and remaining live checks

Operator output supplied in the conversation confirmed three Bound PVCs, ready
Cowrie pods, resource metrics, HPA stabilization, SSH logs and successful original
S3 uploads for all three organisations. Organisation A's deleted pod UID was
included in the original upload. Later B and C Jobs reported seven uploaded files
each under `raw/by-pod/` using their matching uploader identities.

Still require operator evidence rather than assuming success:

- Organisation A's new `raw/by-pod/` upload and downloaded object contents.
- Repeat-upload key-count and changed-content checks against actual S3.
- Full scale-up/down and retained-log verification in each organisation.
- Scheduled execution after explicitly resuming (defaults are paused).
- Team reader account/profile creation and access; no reader profile is required
  on the honeypot operator's computer.
- Actual bucket encryption/versioning/public-access settings and IAM assignments;
  the saved documents record intended settings, not an AWS configuration audit.

Before publication, review `git status` including untracked local overlay files,
stage only intended source/docs/policies/tests, then use a feature branch and PR.
