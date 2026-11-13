"""Nightly raw backup and checkpointed processing of all completed sessions.

Run only on Linux under an exclusive state-volume lock. Pending output bytes and
their UUID are persisted before publication; retries write the same keys/bytes.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from process_replay import process, POD_UID, SESSION_ID, SAFE_ID, MAX_FILE, MAX_LINE, require


def sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def log_paths(root):
    for pod in sorted(root.iterdir()):
        if not POD_UID.fullmatch(pod.name) or pod.is_symlink() or not pod.is_dir():
            continue
        for path in sorted(pod.glob('cowrie.json*')):
            if path.is_file() and not path.is_symlink() and not path.name.startswith('cowrie.json.pipeline-test'):
                yield path


def discover(root, done):
    """Index candidates on temporary disk; validate selected sessions separately.

    Require connect, close, and another event, excluding TCP-only sessions such
    as NLB health checks. Excluded sessions remain in raw backups and are not
    checkpointed. Disk indexing avoids an in-memory candidate-count limit.
    """
    with tempfile.TemporaryDirectory(prefix='cowrie-discovery-') as directory:
        connection = sqlite3.connect(str(Path(directory) / 'candidates.sqlite'))
        try:
            connection.execute('PRAGMA cache_size = -4096')
            connection.execute('CREATE TABLE candidates (key TEXT PRIMARY KEY, flags INTEGER NOT NULL) WITHOUT ROWID')
            with connection:
                connection.executemany(
                    'INSERT INTO candidates VALUES (?, ?) ON CONFLICT(key) DO UPDATE '
                    'SET flags = candidates.flags | excluded.flags',
                    candidate_events(root, done))
            return [row[0] for row in connection.execute(
                'SELECT key FROM candidates WHERE flags = 7 ORDER BY key LIMIT 1000')]
        finally:
            connection.close()


def candidate_events(root, done):
    """Stream boundary and activity flags across rotated logs."""

    for path in log_paths(root):
        require(path.stat().st_size <= MAX_FILE, 'Log exceeds processing size limit')
        opener = gzip.open if path.name.endswith('.gz') else open
        consumed = 0
        with opener(path, 'rb') as handle:
            while line := handle.readline(MAX_LINE + 1):
                consumed += len(line)
                require(len(line) <= MAX_LINE and consumed <= MAX_FILE, 'Decoded log size limit exceeded')
                # An active writer can leave a final partial line; do not select it.
                if not line.endswith(b'\n'):
                    break
                if not line.strip():
                    continue
                event = json.loads(line)
                require(isinstance(event, dict), 'Invalid log record')
                sid = event.get('session')
                if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
                    continue
                key = path.parent.name + '/' + sid
                if key in done:
                    continue
                kind = event.get('eventid')
                if kind == 'cowrie.session.connect':
                    yield key, 1
                elif kind == 'cowrie.session.closed':
                    yield key, 2
                else:
                    yield key, 4



def publish(s3, bucket, org, pending):
    report = json.loads((pending / 'manifest.json').read_text())
    require(bool(SAFE_ID.fullmatch(report['run_id'])), 'Invalid pending run ID')
    require(hashlib.sha256((pending / 'sessions.csv').read_bytes()).hexdigest() == report['csv_sha256'],
            'Pending CSV checksum mismatch; refusing publication')
    prefix = f"{org}/processed/sessions/{report['run_id']}"
    # Replay only persisted bytes at stable keys. A lost acknowledgement or crash
    # after either PUT is safe to retry without creating another batch.
    for name, mime in [('sessions.csv', 'text/csv'), ('manifest.json', 'application/json')]:
        with (pending / name).open('rb') as body:
            s3.put_object(Bucket=bucket, Key=f'{prefix}/{name}', Body=body, ContentType=mime)
    return report


def run(s3, logs, state, org, bucket):
    require(org in ('orga', 'orgb', 'orgc'), 'Invalid organisation')
    state.mkdir(parents=True, exist_ok=True)
    checkpoint = state / 'checkpoint.json'
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        require(saved['organisation'] == org and saved['bucket'] == bucket, 'Checkpoint destination mismatch')
    else:
        saved = dict(version=1, organisation=org, bucket=bucket, processed=[])
        save_json(checkpoint, saved)
    done = set(saved['processed'])
    # Back up originals first, including unfinished sessions; never delete sources.
    count = 0
    for path in log_paths(logs):
        s3.upload_file(str(path), bucket, f'{org}/raw/by-pod/{path.parent.name}/{path.name}')
        count += 1
    print(f'Backed up {count} raw files', flush=True)
    pending = state / 'pending'
    building = state / 'building'
    if not pending.exists():
        keys = discover(logs, done)
        if not keys:
            print('No new completed sessions', flush=True)
            return
        # Only a fixed directory inside the dedicated state volume is removed.
        if building.exists():
            shutil.rmtree(building)
        building.mkdir()
        run_id = uuid.uuid4().hex
        selection = dict(manifest_version=1, organisation=org, run_id=run_id,
                         sessions=[dict(zip(('pod_uid', 'session_id'), key.split('/', 1))) for key in keys])
        save_json(building / 'selection.json', selection)
        report = process(logs, building / 'selection.json', building, org, run_id)
        report['dataset_kind'] = 'all-completed-sessions; replay membership not verified'
        report['destination_bucket'] = bucket
        save_json(building / 'manifest.json', report)
        # Flush outputs before making the pending batch visible to future runs.
        for name in ('sessions.csv', 'manifest.json', 'selection.json'):
            with (building / name).open('r+b') as handle:
                os.fsync(handle.fileno())
        sync_directory(building)
        os.replace(building, pending)
        sync_directory(state)
    pending_report = json.loads((pending / 'manifest.json').read_text())
    require(pending_report['organisation'] == org and pending_report['destination_bucket'] == bucket,
            'Pending batch destination mismatch')
    report = publish(s3, bucket, org, pending)
    done.update(item['pod_uid'] + '/' + item['session_id'] for item in report['sessions'])
    saved['processed'] = sorted(done)
    save_json(checkpoint, saved)
    shutil.rmtree(pending)
    sync_directory(state)
    print(f"Published {report['rows']} sessions; checkpoint contains {len(done)} sessions", flush=True)


def main():
    import fcntl
    import boto3
    from botocore.config import Config
    state = Path('/state')
    state.mkdir(exist_ok=True)
    with (state / 'nightly.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s3 = boto3.client('s3', config=Config(retries={'mode': 'standard', 'max_attempts': 4}))
        run(s3, Path('/logs'), state, os.environ['ORG'], os.environ['S3_BUCKET'])


if __name__ == '__main__':
    main()
