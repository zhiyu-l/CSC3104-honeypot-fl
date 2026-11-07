import csv
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'process_replay.py'
spec = importlib.util.spec_from_file_location('processor', SCRIPT)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
UID = '11111111-1111-1111-1111-111111111111'


def event(kind, second, sid='replay1', **extra):
    return dict(eventid='cowrie.' + kind, session=sid,
                timestamp=f'2026-11-07T00:00:{second:02d}Z', **extra)


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'
        self.pod = self.logs / UID
        self.pod.mkdir(parents=True)
        self.selection = self.root / 'selection.json'
        self.data = dict(manifest_version=1, organisation='orga', run_id='test-001',
                         sessions=[dict(pod_uid=UID, session_id='replay1', expected_label='recon')])
        self.events = [event('session.connect', 0, src_ip='192.0.2.1'),
                       event('login.success', 1), event('command.input', 2, input='whoami'),
                       event('session.closed', 3, duration=3)]

    def write(self, events=None, name='cowrie.json'):
        data = ''.join(json.dumps(e) + '\n' for e in (events if events is not None else self.events))
        if name.endswith('.gz'):
            (self.pod / name).write_bytes(gzip.compress(data.encode()))
        else:
            (self.pod / name).write_text(data)

    def run_process(self):
        self.selection.write_text(json.dumps(self.data))
        return p.process(self.logs, self.selection, self.root / 'out', 'orga', 'test-001')

    def test_exact_shared_features_and_smoke_exclusion(self):
        self.write(self.events + [event('command.input', 2, sid='smoke', input='curl example.invalid')])
        report = self.run_process()
        state = p.extractor._new_session('orgA', UID + ':replay1')
        for e in self.events:
            p.extractor.update(state, e)
        expected = p.extractor.finish(state)
        with (self.root / 'out/sessions.csv').open() as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, p.extractor.META + p.extractor.FEATURES)
            self.assertEqual(list(reader), [{k: str(v) for k, v in expected.items()}])
        self.assertEqual(report['rows'], 1)
        self.assertTrue(report['sessions'][0]['label_matches_expectation'])
        self.assertEqual(report['csv_sha256'], hashlib.sha256((self.root / 'out/sessions.csv').read_bytes()).hexdigest())

    def test_overlap_preserves_same_file_multiplicity(self):
        self.events.insert(3, self.events[2].copy())
        self.write(name='cowrie.json.2026-11-06.gz')
        self.write()
        report = self.run_process()
        self.assertEqual(report['overlap_records_removed'], 5)
        self.assertEqual(report['sessions'][0]['events'], 5)
        with (self.root / 'out/sessions.csv').open() as f:
            self.assertEqual(next(csv.DictReader(f))['n_commands'], '2')

    def test_incomplete_rejected_before_output(self):
        self.write(self.events[:-1])
        with self.assertRaisesRegex(ValueError, 'connect and close'):
            self.run_process()
        self.assertFalse((self.root / 'out/sessions.csv').exists())

    def test_missing_selected_session_rejected(self):
        self.write()
        self.data['sessions'][0]['session_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'connect and close'):
            self.run_process()

    def test_malformed_json_rejected(self):
        (self.pod / 'cowrie.json').write_text('{partial')
        with self.assertRaisesRegex(ValueError, 'Malformed/incomplete'):
            self.run_process()

    def test_path_traversal_rejected(self):
        self.data['sessions'][0]['pod_uid'] = '../other'
        with self.assertRaisesRegex(ValueError, 'Invalid pod_uid'):
            self.run_process()

    def test_expected_label_does_not_override_observed(self):
        self.write()
        self.data['sessions'][0]['expected_label'] = 'persistence'
        report = self.run_process()
        self.assertFalse(report['sessions'][0]['label_matches_expectation'])
        self.assertEqual(report['sessions'][0]['observed_label'], 'recon')

    def test_existing_output_not_overwritten(self):
        self.write()
        self.run_process()
        with self.assertRaisesRegex(ValueError, 'Output already exists'):
            self.run_process()

    def test_duplicate_selection_rejected(self):
        self.data['sessions'].append(self.data['sessions'][0].copy())
        with self.assertRaisesRegex(ValueError, 'Duplicate selected'):
            self.run_process()


if __name__ == '__main__':
    unittest.main()
