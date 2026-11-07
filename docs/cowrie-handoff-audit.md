# Cowrie operator-to-ML handoff audit

Date: 7 November 2026 (Asia/Singapore). Supplements the historical
[3 November audit](cowrie-local-audit.md).

## Current result

The local operator pipeline works through completed-session feature upload for
all three organisations. **Schedules remain paused intentionally.** Teammates can
proceed with reader download/validation and replay identification using the
[handoff guide](cowrie-ml-handoff.md). This does not establish model correctness,
replay provenance, reader credentials or complete processing of the backlog.

## Evidence checked

Live read-only Kubernetes inspection during this audit confirmed:

| Organisation | Cowrie | PVC | CronJob | Successful combined Job |
| --- | --- | --- | --- | --- |
| A | 1/1 Running | cowrie-logs, Bound, 5Gi, RWO | Suspended, no active Jobs | cowrie-upload-20261107191049-9419 |
| B | 1/1 Running | cowrie-logs, Bound, 5Gi, RWO | Suspended, no active Jobs | cowrie-upload-20261107191330-5991 |
| C | 1/1 Running | cowrie-logs, Bound, 5Gi, RWO | Suspended, no active Jobs | cowrie-upload-20261107191337-7959 |

Contexts are `kind-<org>-honeypot-cluster`, namespace `default`, using
`cowrie-nightly:local-v1`. One current Cowrie replica is compatible with the HPA's
1–3 range. The three PVCs are separate despite sharing a name across clusters.

The operator supplied successful logs: A backed up 15 files, B/C 13 each;
each published 1,000 sessions and reported 1,000 checkpoint IDs. The operator
confirmed corresponding objects in S3. A's earlier
`cowrie-upload-20261107190205-3685` failed on the nightly S3 prefix; the later
successful Job confirms recovery after the permission correction. Its remaining
failed pods are historical evidence, not a current running failure.

The final nightly image built and passed 17 synthetic processor/upload/recovery
tests without network and with a read-only container root; 15 Kubernetes rendering
tests passed. Coverage includes feature parity, complete-session selection,
overlap handling, repeat processing, failed publication, checkpoint-commit failure,
corrupt pending output and per-organisation settings. Tests do not prove every
storage failure mode or suitability for unbounded retention.

## Documentation corrections

- Put `processed/sessions/` paths and copy/paste downloads first in the ML guide;
  raw JSON is explicitly an optional investigation path.
- Added checksum/row-count checks, schema details, import deduplication,
  replay mapping requirements and explicit operator/replay/ML ownership.
- Kept separate instructions for manually selected replay and automatic
  completed sessions, scoped IAM policies, state retention and recovery.
- Updated service status to distinguish successful live deployment from the
  earlier offline implementation tests.

Documentation validation during this audit: all relative links in the Cowrie
guides resolved; all 45 PowerShell examples parsed; the new checksum/row-count
example ran successfully against an isolated synthetic batch. A pre-existing
unquoted placeholder in the EKS guide was corrected, and trailing blank lines
from the pause/apply edits were removed. `git diff --check` passed. A targeted
scan of changed/new source files found no AWS access-key IDs or private-key
headers; this is a limited signature check, not a comprehensive secrets audit.

## Remaining checks and boundaries

1. **Reader download needs verification.** This machine has only three uploader
   profiles. The operator reports updating `cowrie-ml-reader` permissions, but
   this audit did not use that reader or inspect live IAM. Test the identity,
   permitted downloads and manifest hashes on the ML teammate's machine.
   `csc3104-logs-login` is a separate console identity; updating the reader user
   does not automatically grant that login access.
2. **The supplied reader policy spans all three organisations.** Use it for
   authorised shared analysis only; isolated FL clients need org-scoped policies.
3. **Data is not verified replay-only.** Initial batches can include historical
   tests. Full backlog size and replay mapping remain unverified. Each invocation
   publishes at most 1,000 new completed sessions.
4. **Schedules stay paused.** New data remains on PVCs until an operator requests
   another manual run. There is currently no scheduled freshness guarantee.
5. **Preserve state and source logs.** `.nightly-state/` shares the log PVC.
   Losing it can cause duplicate publication; deleting a kind cluster can destroy
   local storage. Retention, disk monitoring and backup are operator responsibilities.
   Rotation and event-deduplication limitations are documented in the nightly guide.
6. **Repository handoff is not published yet.** At audit time the implementation,
   policies and docs remain local modified/untracked files. A remote clone will
   not receive them until committed and shared using a feature branch/PR. No
   commit or push was performed by this audit. Exclude credentials, `.local/`
   files and data from the PR.

No schedules were resumed, Jobs triggered, objects uploaded/deleted, IAM policies
changed or secrets retrieved during this audit.
