"""Check the composed kind overlays offline; never contact a cluster or AWS."""
import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class LocalOverlays(unittest.TestCase):
    def test_local_resources_and_organisation_boundaries(self):
        for org in ('orga', 'orgb', 'orgc'):
            with self.subTest(org=org):
                result = subprocess.run(
                    ['kubectl', '--context', f'kind-{org}-honeypot-cluster',
                     'kustomize', str(ROOT / 'deploy/k8s/overlays/local' / org)],
                    check=True, capture_output=True, text=True, encoding='utf-8')
                docs = list(yaml.safe_load_all(result.stdout))
                self.assertFalse(any(d['kind'] in ('Namespace', 'Secret', 'PersistentVolume') for d in docs))
                self.assertTrue(all(d['metadata']['namespace'] == 'default' for d in docs))
                by_kind = {d['kind']: d for d in docs if d['kind'] != 'ConfigMap'}
                maps = {d['metadata']['name']: d for d in docs if d['kind'] == 'ConfigMap'}
                claim = by_kind['PersistentVolumeClaim']
                self.assertEqual(claim['spec']['storageClassName'], 'standard')
                self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
                self.assertEqual(by_kind['Service']['spec']['type'], 'ClusterIP')
                hpa = by_kind['HorizontalPodAutoscaler']['spec']
                self.assertEqual((hpa['minReplicas'], hpa['maxReplicas']), (1, 3))
                pod = by_kind['Deployment']['spec']['template']['spec']
                container = pod['containers'][0]
                self.assertNotIn('envFrom', container)  # AWS credentials belong only to uploader.
                volumes = {v['name']: v for v in pod['volumes']}
                self.assertEqual(volumes['persistent-logs']['persistentVolumeClaim']['claimName'], claim['metadata']['name'])
                log_mount = next(m for m in container['volumeMounts'] if m['name'] == 'persistent-logs')
                self.assertEqual(log_mount['subPathExpr'], '$(POD_UID)')
                config = maps[volumes['config']['configMap']['name']]['data']['cowrie.cfg']
                self.assertIn(f'hostname = {org}-lab', config)
                cron = by_kind['CronJob']['spec']
                self.assertTrue(cron['suspend'])
                self.assertEqual((cron['schedule'], cron['timeZone']), ('0 0 * * *', 'Asia/Singapore'))
                template = cron['jobTemplate']['spec']['template']
                self.assertEqual(template['metadata']['labels']['organisation'], org)
                uploader = template['spec']['containers'][0]
                self.assertEqual(uploader['envFrom'], [{'secretRef': {'name': 'cowrie-aws-credentials'}}])
                self.assertFalse(template['spec']['automountServiceAccountToken'])
                env = {e['name']: e for e in uploader['env']}
                settings = maps[env['S3_BUCKET']['valueFrom']['configMapKeyRef']['name']]['data']
                self.assertEqual(settings['S3_BUCKET'], f'csc3104-cowrie-{org}')
                self.assertEqual(settings['AWS_DEFAULT_REGION'], 'ap-southeast-1')
                upload_volumes = {v['name']: v for v in template['spec']['volumes']}
                self.assertEqual(upload_volumes['logs']['persistentVolumeClaim']['claimName'], claim['metadata']['name'])
                mount = next(m for m in uploader['volumeMounts'] if m['name'] == 'logs')
                self.assertTrue(mount['readOnly'])
                self.assertNotIn('subPath', mount)
                self.assertNotIn('subPathExpr', mount)
                script = maps[upload_volumes['upload-script']['configMap']['name']]['data']['upload.sh']
                self.assertEqual(script, (ROOT / 'services/cowrie/upload.sh').read_text())
                policy = json.loads((ROOT / f'infra/cowrie/local/uploader-{org}.policy.json').read_text())
                self.assertEqual(policy['Statement'][0]['Resource'], f'arn:aws:s3:::csc3104-cowrie-{org}/{org}/raw/*')


if __name__ == '__main__':
    unittest.main()
