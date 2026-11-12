# Recover a batch rejected for a delayed download result

Orgc Job `cowrie-logs-to-s3-29906880` backed up raw logs but repeatedly rejected
session `1553ff45-89ad-4396-9506-4d11e327df57/02b5c1040c40`. The supplied events
close at `2026-11-11T15:23:52.603082Z` and report a download failure at
`15:24:00.095577Z`. This is a processor validation failure, not evidence of faulty
replay or a failed schedule.

The correction allows only `cowrie.session.file_download` and
`cowrie.session.file_download.failed` after closure, retaining feature contributions
without extending connection duration. Exactly one connect/close is still required;
pre-connect events and other post-close events are rejected. Timestamp ties no
longer depend on file order. Manifests record post-close counts, policy and processor
hash. The CSV schema and shared training extractor are unchanged.

Only events present in the input snapshot are included. Events arriving after a
session is checkpointed do not trigger automatic reprocessing. Old published or
persisted pending batches are not rewritten. Preserve raw logs and checkpoints.

## Build on the laptop (PowerShell)

From the updated repository with Docker Desktop running:

```powershell
aws login --profile cowrie-destination
powershell -NoProfile -ExecutionPolicy Bypass -File tools/publish-cowrie-nightly.ps1
```

The helper publishes tag `late-download-v1` to the existing ECR repository and
prints its digest. It uses the temporary Docker-config workaround, retaining the
Buildx plugin directory and deleting the credential file in `finally`. It does
not change your default Docker config or deploy to EKS.

## Update orgc (Bash in CloudShell)

Run after publishing succeeds. This preserves the live schedule and updates only
the two image references. It saves a clean manifest in the original CloudShell
location so a later apply does not restore the old image. Existing Jobs retain
their original images.

```bash
(
set -euo pipefail
[ "$(aws sts get-caller-identity --query Account --output text)" = "909891185696" ]
org=orgc
cluster="${org}-honeypot-cluster"
actual=$(kubectl config view -o "jsonpath={.contexts[?(@.name==\"${cluster}\")].context.cluster}")
[ "$actual" = "arn:aws:eks:ap-southeast-1:909891185696:cluster/${cluster}" ]
digest=$(aws ecr describe-images --region ap-southeast-1 --repository-name cowrie-nightly \
  --image-ids imageTag=late-download-v1 --query 'imageDetails[0].imageDigest' --output text)
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
image="909891185696.dkr.ecr.ap-southeast-1.amazonaws.com/cowrie-nightly@$digest"
mkdir -p "$HOME/cowrie-uploader"
backup="$HOME/cowrie-uploader/${org}-before-late-fix-$(date +%s).json"
kubectl --context "$cluster" -n default get cronjob cowrie-logs-to-s3 -o json > "$backup"
jq -n --arg image "$image" '{spec:{jobTemplate:{spec:{template:{spec:{
  containers:[{name:"uploader",image:$image}],
  initContainers:[{name:"prepare-processor-state",image:$image}]
}}}}}}' > "$HOME/cowrie-uploader/${org}-image-patch.json"
kubectl --context "$cluster" -n default patch cronjob cowrie-logs-to-s3 --type=strategic \
  --patch-file "$HOME/cowrie-uploader/${org}-image-patch.json"
kubectl --context "$cluster" -n default get cronjob cowrie-logs-to-s3 -o json |
  jq '{apiVersion,kind,metadata:{name:.metadata.name,namespace:.metadata.namespace,labels:.metadata.labels},spec}' \
  > "$HOME/cowrie-uploader/${org}-nightly.json"
echo "Backup: $backup"
echo "New image: $image"
)
```

Inspect the backup for rollback image references; the old image will still reject
this session. Check no processor Job is active before starting another manual Job.

```bash
cluster=orgc-honeypot-cluster
kubectl --context "$cluster" -n default get jobs
job="cowrie-late-fix-$(date +%s)"
kubectl --context "$cluster" -n default create job "$job" --from=cronjob/cowrie-logs-to-s3
kubectl --context "$cluster" -n default wait --for=condition=complete "job/$job" --timeout=1200s
kubectl --context "$cluster" -n default logs "job/$job" -c uploader
aws s3 ls s3://csc3104-cowrie-orgc-909891185696/orgc/processed/sessions/ --recursive
```

A wait timeout does not cancel the Job; inspect before retrying. Expect a
publication and checkpoint advance. Verify the new CSV hash against its manifest,
and inspect `post_close_events` for the affected session if selected. Other input
validation errors remain possible and must not be suppressed.

After orgc passes, repeat the update/test for orga and orgb using their explicit
organisation/context. Record the new digest in renderer settings as `nightly_image`,
preserving `schedule_enabled`. No IAM change, raw-log edit or checkpoint reset is
needed. This is a repository fix until the new image is published and deployed.
