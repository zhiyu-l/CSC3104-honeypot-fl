set -eu

: "${ORG:?ORG is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"
: "${UPLOAD_POD_UID:?UPLOAD_POD_UID is required}"

case "$ORG" in
  orga|orgb|orgc) ;;
  *) echo "Unexpected organisation: $ORG"; exit 1 ;;
esac

aws sts get-caller-identity

# Use UTC consistently for the folder and upload timestamp.
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
UPLOAD_DATE=${STAMP%%T*}
RUN_ID="${STAMP}-${UPLOAD_POD_UID}"
DEST="s3://${S3_BUCKET}/${ORG}/raw/${UPLOAD_DATE}"

COUNT=0

for POD_DIR in /logs/*; do
  [ -d "$POD_DIR" ] || continue
  [ ! -L "$POD_DIR" ] || continue

  SOURCE_POD_UID=${POD_DIR##*/}

  for FILE in "$POD_DIR"/cowrie.json*; do
    [ -f "$FILE" ] || continue
    [ ! -L "$FILE" ] || continue

    ORIGINAL_NAME=${FILE##*/}

    # Exclude the synthetic pipeline test file.
    case "$ORIGINAL_NAME" in
      cowrie.json.pipeline-test*) continue ;;
    esac

    OBJECT_NAME="${RUN_ID}_${SOURCE_POD_UID}_${ORIGINAL_NAME}"

    aws s3 cp "$FILE" "${DEST}/${OBJECT_NAME}" \
      --no-follow-symlinks \
      --no-progress \
      --only-show-errors

    COUNT=$((COUNT + 1))
    echo "Uploaded: ${OBJECT_NAME}"
  done
done

if [ "$COUNT" -eq 0 ]; then
  echo "ERROR: No matching log files found."
  exit 1
fi

echo "SUCCESS: Uploaded $COUNT files to ${DEST}/"
