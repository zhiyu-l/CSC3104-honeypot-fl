# Cowrie replay log processor

For the current combined nightly CronJob, use [the nightly guide](../../docs/cowrie-nightly.md).
`nightly.py` adds discovery, raw backup, persistent pending batches, recovery and
checkpoints. Build it with `nightly.Dockerfile`; the original `Dockerfile` below
remains for manually selected replay Jobs.

Follow [the operator guide](../../docs/cowrie-feature-processing.md) for all three
organisations, selection format, image loading, IAM updates and ML handoff.

- `process_replay.py`: validates selected completed sessions and invokes the
  existing `tools/cowrie_dataset/cowrie_sessions.py` extractor.
- `Dockerfile`: standard-library-only, non-root processing image.
- `upload_features.sh`: conditional S3 uploads, completion manifest written last.
- `replay-selection.example.json`: selection template; replace placeholders.
- `tests/`: synthetic feature, filtering, duplication and upload-failure tests.

The Kubernetes helper is `deploy/k8s/overlays/local/process-replay.ps1`. It renders
or creates a manual Job. The local organisation overlay patches separately
configure the existing CronJob to run the combined nightly image while paused.

Validated locally on 2026-11-07: 17 processor/uploader/recovery tests inside the built
image with networking disabled and a read-only root filesystem, plus 15
Kubernetes rendering/configuration tests. The combined nightly image built
successfully. The operator subsequently deployed it and published 1,000 sessions
for each organisation; see the [handoff audit](../../docs/cowrie-handoff-audit.md).
Schedules remain paused. New installations still require IAM setup, image loading
and deployment. Manual selected-replay Jobs additionally need replay selections.
