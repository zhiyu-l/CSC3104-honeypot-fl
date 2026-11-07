set -eu
: "${ORG:?ORG is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"
case "$ORG" in orga|orgb|orgc) ;; *) echo 'Invalid organisation'; exit 1 ;; esac
case "$RUN_ID" in ''|*[!a-z0-9-]*) echo 'Invalid run ID'; exit 1 ;; esac
PREFIX="${ORG}/processed/replay/${RUN_ID}"
# Only upload a validated completed output set. manifest.json is the completion
# marker: consumers must not ingest a run until it exists and its hash matches.
test -s /output/sessions.csv
test -s /output/manifest.json
aws s3api put-object --bucket "$S3_BUCKET" --key "$PREFIX/sessions.csv" \
  --body /output/sessions.csv --content-type text/csv --if-none-match '*' --no-cli-pager >/dev/null
aws s3api put-object --bucket "$S3_BUCKET" --key "$PREFIX/manifest.json" \
  --body /output/manifest.json --content-type application/json --if-none-match '*' --no-cli-pager >/dev/null
echo "SUCCESS: Published completed run to s3://${S3_BUCKET}/${PREFIX}/"
