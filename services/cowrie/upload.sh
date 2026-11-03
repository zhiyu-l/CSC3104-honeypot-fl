set -eu

: "${ORG:?ORG is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"

case "$ORG" in
  orga|orgb|orgc) ;;
  *) echo "Unexpected organisation: $ORG"; exit 1 ;;
esac

aws sts get-caller-identity

# Stable keys isolate this layout from legacy dated snapshots. Repeated uploads
# overwrite the same object; this does not deduplicate events across log rotation.
DEST="s3://${S3_BUCKET}/${ORG}/raw/by-pod"
COUNT=0

for POD_DIR in /logs/*; do
  [ -d "$POD_DIR" ] || continue
  [ ! -L "$POD_DIR" ] || continue

  SOURCE_POD_UID=${POD_DIR##*/}

  for FILE in "$POD_DIR"/cowrie.json*; do
    [ -f "$FILE" ] || continue
    [ ! -L "$FILE" ] || continue

    ORIGINAL_NAME=${FILE##*/}

    case "$ORIGINAL_NAME" in
      cowrie.json.pipeline-test*) continue ;;
    esac

    OBJECT_NAME="${SOURCE_POD_UID}/${ORIGINAL_NAME}"

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
