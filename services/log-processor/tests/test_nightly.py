import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nightly
from test_processor import UID, event


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.fail = None
        self.raw = []

    def upload_file(self, path, bucket, key):
        self.raw.append(key)

    def put_object(self, Bucket, Key, Body, ContentType):
        data = Body.read()
        if Key.endswith(self.fail or 'NEVER'):
            raise OSError('Injected upload failure')
        self.objects[Key] = data
        self.writes.append((Key, data))


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'
        (self.logs / UID).mkdir(parents=True)
        self.state = self.root / 'state'
        self.s3 = FakeS3()
        self.events = [event('session.connect', 0), event('command.input', 1, input='whoami'),
                       event('session.closed', 2), event('session.connect', 3, sid='active')]
        self.write()

    def write(self):
        (self.logs / UID / 'cowrie.json').write_text(''.join(json.dumps(e)+'\n' for e in self.events))

    def run_batch(self):
        nightly.run(self.s3, self.logs, self.state, 'orga', 'bucket-a')

    def test_skip_published_and_wait_for_active_session(self):
        self.run_batch()
        self.assertEqual(len(self.s3.writes), 2)
        self.run_batch()
        self.assertEqual(len(self.s3.writes), 2)
        self.assertEqual(len(self.s3.raw), 2)
        self.events.append(event('session.closed', 5, sid='active'))
        self.write()
        self.run_batch()
        self.assertEqual(len(self.s3.objects), 4)
        self.assertEqual(len(json.loads((self.state/'checkpoint.json').read_text())['processed']), 2)

    def test_upload_failure_preserves_pending_bytes(self):
        self.s3.fail = 'manifest.json'
        with self.assertRaises(OSError):
            self.run_batch()
        self.assertEqual(json.loads((self.state/'checkpoint.json').read_text())['processed'], [])
        first = self.s3.writes[0]
        self.s3.fail = None
        self.run_batch()
        self.assertEqual(self.s3.writes[1], first)
        self.assertEqual(len(self.s3.objects), 2)
        self.assertFalse((self.state/'pending').exists())

    def test_crash_after_publication_reuses_same_objects(self):
        original = nightly.save_json
        def fail_checkpoint(path, data):
            if path.name == 'checkpoint.json' and data.get('processed'):
                raise OSError('Injected checkpoint failure')
            original(path, data)
        with patch.object(nightly, 'save_json', side_effect=fail_checkpoint):
            with self.assertRaises(OSError):
                self.run_batch()
        before = dict(self.s3.objects)
        self.run_batch()
        self.assertEqual(self.s3.objects, before)
        self.assertFalse((self.state/'pending').exists())

    def test_different_destination_rejected(self):
        self.run_batch()
        with self.assertRaisesRegex(ValueError, 'destination mismatch'):
            nightly.run(self.s3, self.logs, self.state, 'orgb', 'bucket-b')

    def test_no_completed_sessions_only_raw_backup(self):
        self.events = [event('session.connect', 0)]
        self.write()
        self.run_batch()
        self.assertTrue(self.s3.raw)
        self.assertFalse(self.s3.objects)

    def test_corrupt_pending_batch_not_published(self):
        self.s3.fail = 'sessions.csv'
        with self.assertRaises(OSError):
            self.run_batch()
        (self.state/'pending/sessions.csv').write_text('corrupt')
        self.s3.fail = None
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.run_batch()
        self.assertFalse(self.s3.objects)
