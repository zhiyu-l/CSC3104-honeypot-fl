"""Produce a bounded replay dataset using the exact Kaggle session extractor.

Standard library only. No AWS credentials or API access required. Selection is
an explicit (pod UID, new Cowrie session ID) manifest supplied by the replay owner.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/cowrie_dataset'))
sys.path.insert(0, str(ROOT / 'libs/fl-common/src'))
import cowrie_sessions as extractor  # noqa: E402
from fl_common.schema import (  # noqa: E402
    FEATURE_NAMES, SCHEMA_VERSION, EXCLUDED_LABELS, LABEL_NAMES,
    label_session, parse_label, validate_row,
)

ORG_NAMES = {'orga': 'orgA', 'orgb': 'orgB', 'orgc': 'orgC'}
SAFE_ID = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,34}[a-z0-9])?\Z')
POD_UID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
SESSION_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}\Z')
MAX_LINE = 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_EVENTS = 250_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


def load_selection(path: Path, org: str, run_id: str):
    require(path.stat().st_size <= 512 * 1024, 'Selection exceeds 512 KiB ConfigMap budget')
    selection = json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=reject_constant)
    require(isinstance(selection, dict), 'Selection must be an object')
    require(selection.get('manifest_version') == 1, 'Expected manifest_version 1')
    require(org in ORG_NAMES and selection.get('organisation') == org, 'Organisation mismatch')
    require(bool(SAFE_ID.fullmatch(run_id)) and selection.get('run_id') == run_id, 'Invalid or mismatched run_id')
    sessions = selection.get('sessions')
    require(isinstance(sessions, list) and 0 < len(sessions) <= 10_000, 'Select 1-10000 sessions')
    selected = {}
    for item in sessions:
        require(isinstance(item, dict), 'Session selection must be an object')
        uid, sid = item.get('pod_uid'), item.get('session_id')
        require(isinstance(uid, str) and bool(POD_UID.fullmatch(uid)), 'Invalid pod_uid')
        require(isinstance(sid, str) and bool(SESSION_ID.fullmatch(sid)), 'Invalid session_id')
        require((uid, sid) not in selected, 'Duplicate selected session')
        if 'expected_label' in item:
            require(item['expected_label'] in set(LABEL_NAMES) | EXCLUDED_LABELS, 'Unknown expected_label')
        if 'source_session_id' in item:
            require(isinstance(item['source_session_id'], str) and len(item['source_session_id']) <= 512,
                    'Invalid original dataset session reference')
        selected[uid, sid] = item
    return selection, selected


def validate_event(event):
    for name in ('eventid', 'timestamp'):
        require(isinstance(event.get(name), str), f'Selected event missing string {name}')
    for name in ('input', 'src_ip', 'url'):
        require(name not in event or event[name] is None or isinstance(event[name], str), f'Invalid selected event {name}')
    require(event.get('input', '') is not None, 'Null command input')
    stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
    require(stamp.tzinfo is not None, 'Selected timestamps must include a timezone')
    if 'duration' in event and event['duration'] is not None:
        duration = event['duration']
        require(isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and math.isfinite(duration) and duration >= 0, 'Invalid selected session duration')
    return stamp.timestamp()


def process(log_root: Path, selection_path: Path, output: Path, org: str, run_id: str):
    selection, selected = load_selection(selection_path, org, run_id)
    require(tuple(extractor.FEATURES) == FEATURE_NAMES, 'Extractor/schema feature mismatch')
    output.mkdir(parents=True, exist_ok=True)
    require(not any((output / name).exists() for name in ('sessions.csv', 'manifest.json')),
            'Output already exists; use a new output directory/run ID')
    root = log_root.resolve(strict=True)
    events = defaultdict(list)
    seen = defaultdict(Counter)
    sources, removed = [], 0
    budget = 0
    for uid in sorted({uid for uid, _ in selected}):
        directory = root / uid
        require(directory.is_dir() and not directory.is_symlink(), f'Missing or symlinked pod folder: {uid}')
        require(directory.resolve().parent == root, 'Pod folder escapes log root')
        paths = [Path(p) for p in extractor.log_files(str(directory))]
        for path in paths:
            if path.name.startswith('cowrie.json.pipeline-test') or path.is_symlink():
                continue
            require(path.is_file() and path.resolve().parent == directory.resolve(), 'Unsafe log file')
            counts = defaultdict(Counter)
            digest = hashlib.sha256()
            # Freeze the initial byte length through one open handle; later appends
            # are for a future run. Gzip files are bounded again when decompressed.
            with path.open('rb') as source, tempfile.TemporaryFile() as snapshot:
                size = os.fstat(source.fileno()).st_size
                require(size <= MAX_FILE, 'Source file exceeds 512 MiB limit')
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    require(bool(chunk), 'Source truncated during snapshot; retry after replay finishes')
                    digest.update(chunk)
                    snapshot.write(chunk)
                    remaining -= len(chunk)
                snapshot.seek(0)
                stream = gzip.GzipFile(fileobj=snapshot) if path.name.endswith('.gz') else snapshot
                consumed = 0
                try:
                    while True:
                        line = stream.readline(MAX_LINE + 1)
                        if not line:
                            break
                        consumed += len(line)
                        require(consumed <= MAX_FILE and len(line) <= MAX_LINE, 'Decoded log size/line limit exceeded')
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line, parse_constant=reject_constant)
                        except (ValueError, UnicodeError):
                            raise ValueError(f'Malformed/incomplete JSON in {uid}/{path.name}; retry with complete logs') from None
                        require(isinstance(event, dict), 'Log event must be an object')
                        sid = event.get('session')
                        if not isinstance(sid, str) or (uid, sid) not in selected:
                            continue
                        stamp = validate_event(event)
                        key = (uid, sid)
                        signature = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':'),
                                                              allow_nan=False).encode()).hexdigest()
                        counts[key][signature] += 1
                        # Across overlapping files retain the maximum multiplicity,
                        # preserving repeated identical records within a single file.
                        if counts[key][signature] <= seen[key][signature]:
                            removed += 1
                            continue
                        budget += 1
                        require(budget <= MAX_EVENTS, 'Selected event budget exceeded; split the run')
                        events[key].append((stamp, event))
                finally:
                    if stream is not snapshot:
                        stream.close()
            for key, count in counts.items():
                seen[key] |= count
            sources.append({'path': f'{uid}/{path.name}', 'bytes': size, 'sha256': digest.hexdigest()})

    rows, outcomes = [], []
    for key, item in selected.items():
        uid, sid = key
        ordered = sorted(events[key], key=lambda pair: pair[0])
        kinds = [e['eventid'] for _, e in ordered]
        require(kinds.count('cowrie.session.connect') == 1 and kinds.count('cowrie.session.closed') == 1,
                f'Session {uid}/{sid} needs exactly one connect and close event; missing/incomplete/reused ID')
        require(kinds[0] == 'cowrie.session.connect' and kinds[-1] == 'cowrie.session.closed',
                f'Session {uid}/{sid} has events outside its lifetime')
        state = extractor._new_session(ORG_NAMES[org], f'{uid}:{sid}')
        for _, event in ordered:
            extractor.update(state, event)
        row = extractor.finish(state)
        feature_values = {name: row[name] for name in FEATURE_NAMES}
        validate_row(feature_values)  # CSV stays raw: client/evaluator clip and transform once.
        require(parse_label(row['label']) == label_session(feature_values), 'Extractor/schema label mismatch')
        rows.append(row)
        outcome = dict(item, output_session_id=row['session_id'], observed_label=row['label'], events=len(ordered))
        if 'expected_label' in item:
            outcome['label_matches_expectation'] = item['expected_label'] == row['label']
        outcomes.append(outcome)

    csv_path = output / 'sessions.csv'
    with csv_path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=extractor.META + extractor.FEATURES)
        writer.writeheader()
        writer.writerows(rows)
    report = {
        'manifest_version': 1, 'organisation': org, 'run_id': run_id,
        'schema_version': SCHEMA_VERSION, 'feature_names': list(FEATURE_NAMES),
        'created_at': datetime.now(timezone.utc).isoformat(), 'rows': len(rows),
        'label_counts': dict(Counter(row['label'] for row in rows)),
        'excluded_from_current_model': sorted(EXCLUDED_LABELS),
        'overlap_records_removed': removed,
        'deduplication': 'maximum full-event multiplicity across files within each pod/session; heuristic, not event IDs',
        'csv_sha256': hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        'selection_sha256': hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest(),
        'extractor_sha256': hashlib.sha256(Path(extractor.__file__).read_bytes()).hexdigest(),
        'sources': sources, 'sessions': outcomes,
        'labels': 'observed rule-based labels; expected replay labels are recorded separately',
    }
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--org', choices=ORG_NAMES, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        report = process(args.logs, args.selection, args.out, args.org, args.run_id)
    except (ValueError, OSError, TypeError, KeyError, EOFError) as error:
        parser.exit(1, f'Processing failed: {error}\n')
    print(f"Produced {report['rows']} session rows for {args.org}/{args.run_id}")


if __name__ == '__main__':
    main()
