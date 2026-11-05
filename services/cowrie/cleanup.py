import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/logs")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
cutoff = time.time() - RETENTION_DAYS * 86400

# Only recognise dated Cowrie JSON rotations.
# Active cowrie.json and pipeline-test files are excluded.
rotated_name = re.compile(
    r"^cowrie\.json[.-]\d{4}-\d{2}-\d{2}(?:\.gz)?$"
)

candidates = 0
errors = 0

print(f"DRY RUN: checking rotated logs older than {RETENTION_DAYS} days")
print("No files will be deleted.", flush=True)

def walk_error(error):
    global errors
    errors += 1
    print(f"ERROR: {error}", flush=True)

for directory, _, filenames in os.walk(
    ROOT, followlinks=False, onerror=walk_error
):
    for filename in filenames:
        if not rotated_name.fullmatch(filename):
            continue

        path = Path(directory) / filename

        try:
            if path.is_symlink() or not path.is_file():
                continue

            info = path.stat()
            if info.st_mtime >= cutoff:
                continue

            modified = datetime.fromtimestamp(
                info.st_mtime, timezone.utc
            ).isoformat()

            candidates += 1
            print(
                f"REVIEW: {path.relative_to(ROOT)} "
                f"| bytes={info.st_size} "
                f"| modified={modified} "
                "| S3 verification still required",
                flush=True,
            )
        except FileNotFoundError:
            print(f"SKIPPED: file disappeared during scan: {path}")
        except OSError as error:
            errors += 1
            print(f"ERROR: {path}: {error}", flush=True)

print(f"Finished: {candidates} candidate files; {errors} errors.")
print("Candidates are NOT approved for deletion.")
raise SystemExit(1 if errors else 0)
