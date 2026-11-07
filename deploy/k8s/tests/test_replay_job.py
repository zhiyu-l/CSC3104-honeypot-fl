"""Render replay Jobs offline and check organisation and credential boundaries."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[3]


class ReplayJobTests(unittest.TestCase):
    def test_render_three_organisations(self):
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if not shell:
            self.skipTest('PowerShell not installed')
        for org in ('orga', 'orgb', 'orgc'):
            with self.subTest(org=org), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'selection.json'
                path.write_text(json.dumps(dict(manifest_version=1, organisation=org,
                    run_id='offline-render-test', sessions=[dict(
                    pod_uid='11111111-1111-1111-1111-111111111111', session_id='test1')])) )
                subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(ROOT / 'deploy/k8s/overlays/local/process-replay.ps1'), '-Action', 'render',
                    '-Org', org, '-Manifest', str(path)], check=True, capture_output=True)
                rendered = ROOT / '.local/cowrie/processed-runs' / org / 'offline-render-test'
                job = yaml.safe_load((rendered / 'job.yaml').read_text())
                cm = json.loads((rendered / 'selection.configmap.json').read_text())
                self.assertTrue(cm['immutable'])
                self.assertEqual(json.loads(cm['data']['replay.json'])['organisation'], org)
                self.assertEqual(job['metadata']['namespace'], 'default')
                self.assertEqual(job['spec']['backoffLimit'], 0)
                pod = job['spec']['template']['spec']
                processor, uploader = pod['initContainers'][0], pod['containers'][0]
                self.assertEqual(processor['name'], 'process')
                self.assertNotIn('envFrom', processor)
                self.assertTrue(next(m for m in processor['volumeMounts'] if m['name'] == 'logs')['readOnly'])
                self.assertNotIn('logs', [m['name'] for m in uploader['volumeMounts']])
                env = {item['name']: item['value'] for item in uploader['env']}
                self.assertEqual(env['S3_BUCKET'], f'csc3104-cowrie-{org}')
                self.assertEqual(env['ORG'], org)
                self.assertEqual(uploader['envFrom'][0]['secretRef']['name'], 'cowrie-aws-credentials')
                policy = json.loads((ROOT / f'infra/cowrie/local/uploader-{org}.policy.json').read_text())
                statement = next(s for s in policy['Statement'] if s['Sid'] == 'UploadOrganisationFeatures')
                self.assertEqual(statement['Resource'], f'arn:aws:s3:::csc3104-cowrie-{org}/{org}/processed/replay/*')


if __name__ == '__main__':
    unittest.main()
