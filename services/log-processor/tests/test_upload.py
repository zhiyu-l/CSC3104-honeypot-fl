"""Exercise the shell uploader with a fake AWS CLI; never contact AWS."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UploadTests(unittest.TestCase):
    def check_upload(self, fail):
        bash = r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt' else shutil.which('bash')
        if not bash or not Path(bash).exists():
            self.skipTest('bash unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sessions.csv').write_text('header\nrow\n')
            (root / 'manifest.json').write_text('{}')
            script = (Path(__file__).resolve().parents[1] / 'upload_features.sh').read_text()
            (root / 'upload.sh').write_text(script.replace('/output/', './'), newline='\n')
            fake = '''aws() {
    printf '%s\\n' "$*" >> calls.txt
    if [ "$FAIL" = yes ]; then return 1; fi
}
export -f aws
bash upload.sh
'''
            result = subprocess.run([bash, '-c', fake], cwd=root, capture_output=True,
                env=dict(os.environ, ORG='orgb', RUN_ID='test-001', S3_BUCKET='test-bucket', FAIL=fail))
            calls = (root / 'calls.txt').read_text().splitlines()
            self.assertEqual(len(calls), 1 if fail == 'yes' else 2)
            self.assertIn('orgb/processed/replay/test-001/sessions.csv', calls[0])
            self.assertTrue(all('--if-none-match *' in call for call in calls))
            if fail == 'yes':
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'SUCCESS', result.stdout)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('orgb/processed/replay/test-001/manifest.json', calls[1])

    def test_completion_marker_written_last(self):
        self.check_upload('no')

    def test_csv_failure_prevents_completion_marker(self):
        self.check_upload('yes')
