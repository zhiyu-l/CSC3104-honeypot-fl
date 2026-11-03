"""Offline cluster-boundary regressions; kubectl is only real for local Kustomize builds."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("cowrie_config", ROOT / "deploy/k8s/cowrie_config.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


class ClusterBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {org: config.build(org) for org in config.ORGS}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cowrie-cluster-test-")
        self.local = Path(self.temporary.name)
        self.local_patch = patch.object(config, "LOCAL", self.local)
        self.local_patch.start()
        self.settings = {
            "aws_region": "ap-southeast-1", "s3_bucket": "cowrie-example-bucket",
            "efs_filesystem_id": "fs-00000000000000000",
            "organisations": {
                org: {"cluster_name": cluster, "kubectl_context": cluster,
                      "cluster_arn": f"arn:aws:eks:ap-southeast-1:000000000000:cluster/{cluster}",
                      "efs_access_point_id": f"fsap-{i:017x}",
                      "allowed_source_cidrs": ["192.0.2.1/32"],
                      "uploader_role_arn": f"arn:aws:iam::000000000000:role/example-{org}"}
                for i, (org, cluster) in enumerate(config.CLUSTERS.items(), 1)
            },
        }
        self.settings_path = self.local / "settings.json"
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")

    def tearDown(self):
        self.local_patch.stop()
        self.temporary.cleanup()

    def render(self):
        with contextlib.redirect_stdout(io.StringIO()):
            config.render(self.settings_path, config.ORGS)

    def test_each_cluster_validates_independently(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for org, docs in self.documents.items():
                config.validate(docs, org, allow_placeholders=True)
                self.assertEqual(len(docs), 11)
                self.assertFalse(any(d["kind"] == "Namespace" for d in docs))

    def test_offline_build_has_explicit_context_and_namespace(self):
        for org, cluster in config.CLUSTERS.items():
            with patch.object(config.subprocess, "run", return_value=SimpleNamespace(stdout="")) as run:
                config.build(org)
            self.assertEqual(run.call_args.args[0], [
                "kubectl", "--context", cluster, "--namespace", "default",
                "kustomize", str(config.OVERLAY / org),
            ])

    def test_render_separates_targets_and_aws_identity(self):
        self.render()
        for org in config.ORGS:
            directory = self.local / "rendered" / org
            docs = [d for group in config.GROUPS for d in yaml.safe_load_all(
                (directory / f"{group}.yaml").read_text())]
            with contextlib.redirect_stdout(io.StringIO()):
                config.validate(docs, org)
            self.assertEqual(json.loads((directory / "target.json").read_text()), config.target_for(self.settings, org))
            self.assertFalse((directory / "namespaces.yaml").exists())
            identity = directory / "infra"
            association = json.loads((identity / "pod-identity-association.json").read_text())
            self.assertEqual(association, {"clusterName": config.CLUSTERS[org], "namespace": "default",
                "serviceAccount": "cowrie-log-uploader", "roleArn": self.settings["organisations"][org]["uploader_role_arn"]})
            trust = json.loads((identity / "pod-identity-trust.json").read_text())["Statement"][0]["Condition"]["StringEquals"]
            self.assertEqual(trust["aws:RequestTag/kubernetes-namespace"], "default")
            self.assertEqual(trust["aws:RequestTag/eks-cluster-arn"], self.settings["organisations"][org]["cluster_arn"])
            policy = json.loads((identity / "uploader-policy.json").read_text())
            self.assertTrue(policy["Statement"][0]["Resource"].endswith(f"/{org}/raw/*"))
        self.assertFalse((self.local / "rendered" / "workloads.yaml").exists())

    def test_mixed_cluster_manifests_rejected(self):
        with self.assertRaisesRegex(ValueError, "Mixed organisation"):
            config.validate(self.documents["orga"] + self.documents["orgb"], "orga", allow_placeholders=True)

    def test_old_namespace_claim_and_uploader_identity_rejected(self):
        changes = {
            "namespace": lambda docs: docs[0]["metadata"].update(namespace="orga"),
            "claim": lambda docs: next(d for d in docs if d["kind"] == "PersistentVolume")["spec"]["claimRef"].update(namespace="orga"),
            "uploader identity": lambda docs: next(d for d in docs if d["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["fieldRef"].update(fieldPath="metadata.namespace"),
            "scale-down": lambda docs: next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")["spec"]["behavior"]["scaleDown"].update(selectPolicy="Disabled"),
        }
        for name, mutate in changes.items():
            with self.subTest(name=name):
                docs = copy.deepcopy(self.documents["orga"])
                mutate(docs)
                with self.assertRaises(ValueError):
                    config.validate(docs, "orga", allow_placeholders=True)

    def test_wrong_cluster_context_and_shared_role_settings_rejected(self):
        mutations = (
            lambda s: s["organisations"]["orga"].update(kubectl_context=config.CLUSTERS["orgb"]),
            lambda s: s["organisations"]["orga"].update(cluster_arn=s["organisations"]["orgb"]["cluster_arn"]),
            lambda s: s["organisations"]["orga"].update(uploader_role_arn=s["organisations"]["orgb"]["uploader_role_arn"]),
        )
        for mutate in mutations:
            settings = copy.deepcopy(self.settings)
            mutate(settings)
            self.settings_path.write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                config.load_settings(self.settings_path)

    def test_context_mismatch_prevents_operational_command(self):
        wrong = self.settings["organisations"]["orgb"]["cluster_arn"]
        with patch.object(config.subprocess, "run", return_value=SimpleNamespace(stdout=wrong)) as run:
            with self.assertRaisesRegex(ValueError, "refusing operation"):
                config.run_kubectl(self.settings, "orga", ["get", "pods"])
        self.assertEqual(run.call_count, 1)  # Local kubeconfig check only.

    def test_operational_command_is_explicit_for_every_cluster(self):
        for org, cluster in config.CLUSTERS.items():
            result = SimpleNamespace(stdout=self.settings["organisations"][org]["cluster_arn"])
            with patch.object(config.subprocess, "run", side_effect=[result, SimpleNamespace(returncode=0)]) as run:
                self.assertEqual(config.run_kubectl(self.settings, org, ["get", "pods"]), 0)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:5], ["kubectl", "--context", cluster, "--namespace", "default"])
            self.assertEqual(run.call_args.args[0][5:], ["get", "pods"])

    def test_context_and_namespace_overrides_rejected_without_calls(self):
        for flags in (["--context", "other"], ["--namespace=orga"], ["-norga"],
                      ["--kubeconfig", "other"], ["--server=other"], ["--all-namespaces"],
                      ["-k", "templates"]):
            with self.subTest(flags=flags), patch.object(config.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    config.run_kubectl(self.settings, "orga", ["get", "pods", *flags])
                run.assert_not_called()

    def test_foreign_and_combined_files_rejected_before_context_check(self):
        for filename in (self.local / "rendered/orgb/workloads.yaml", self.local / "rendered/workloads.yaml", "-"):
            with patch.object(config.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    config.run_kubectl(self.settings, "orga", ["apply", "-f", str(filename)])
                run.assert_not_called()

    def test_stale_target_and_tampered_manifest_rejected(self):
        self.render()
        directory = self.local / "rendered/orga"
        target = directory / "target.json"
        original = target.read_text()
        target.write_text(json.dumps(config.target_for(self.settings, "orgb")))
        with self.assertRaisesRegex(ValueError, "target is stale"):
            config.check_file_target("orga", directory / "workloads.yaml", self.settings)
        target.write_text(original)
        path = directory / "workloads.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        docs[0]["metadata"]["labels"]["organisation"] = "orgb"
        path.write_text(yaml.safe_dump_all(docs))
        with self.assertRaisesRegex(ValueError, "another organisation"):
            config.check_file_target("orga", path, self.settings)

    def test_cli_requires_organisation_and_preserves_command_arguments(self):
        argv = ["cowrie_config.py", "kubectl", "--org", "orga", "--settings", str(self.settings_path), "--", "get", "pods"]
        with patch.object(sys, "argv", argv), patch.object(config, "run_kubectl", return_value=0) as run:
            with self.assertRaises(SystemExit) as exit_result:
                config.main()
            self.assertEqual(exit_result.exception.code, 0)
        self.assertEqual(run.call_args.args[1:], ("orga", ["--", "get", "pods"]))

    def test_default_namespace_uploads_keep_org_prefix_and_deleted_pod_logs(self):
        shell = shutil.which("sh")
        if not shell and shutil.which("git"):
            candidate = Path(shutil.which("git")).parent.parent / "bin/bash.exe"
            shell = str(candidate) if candidate.exists() else None
        if not shell:
            self.skipTest("POSIX shell needed for the local uploader execution check")
        logs = self.local / "logs"
        for uid in ("current-pod-uid", "deleted-pod-uid"):
            directory = logs / uid
            directory.mkdir(parents=True)
            (directory / "cowrie.json").write_text(json.dumps({"eventid": "cowrie.test", "source": uid}))
            (directory / "cowrie.json.pipeline-test-ignore").write_text("synthetic excluded file")
        before = {str(p.relative_to(logs)): p.read_bytes() for p in logs.rglob("*") if p.is_file()}
        # Substitute only the mount root, and replace AWS with a local shell stub.
        script = (ROOT / "services/cowrie/upload.sh").read_text().replace("/logs/*", '"$TEST_LOG_ROOT"/*')
        stub = '''aws() {
 case "$1 $2" in
   "sts get-caller-identity") printf '{}\\n' ;;
   "s3 cp") printf '%s\\n' "$4" >> "$AUDIT"; cp "$3" "$CAPTURE_DIR/${4##*/}" ;;
   *) return 99 ;;
 esac
}
'''
        path = self.local / "upload-test.sh"
        path.write_text(stub + script, encoding="utf-8", newline="\n")
        for org, docs in self.documents.items():
            cron = next(d for d in docs if d["kind"] == "CronJob")
            pod = cron["spec"]["jobTemplate"]["spec"]["template"]
            self.assertEqual(cron["metadata"]["namespace"], "default")
            identity = pod["metadata"]["labels"]["organisation"]
            capture = self.local / org
            capture.mkdir()
            audit = self.local / f"{org}-audit.txt"
            environment = dict(os.environ, ORG=identity, S3_BUCKET="cowrie-example-bucket",
                UPLOAD_POD_UID="uploader-test-uid", TEST_LOG_ROOT=logs.as_posix(),
                CAPTURE_DIR=capture.as_posix(), AUDIT=audit.as_posix())
            result = subprocess.run([shell, path.as_posix()], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            objects = audit.read_text().splitlines()
            self.assertEqual(len(objects), 2)
            self.assertTrue(all(f"/{org}/raw/" in name and "/default/raw/" not in name for name in objects))
            self.assertTrue(any("deleted-pod-uid_cowrie.json" in name for name in objects))
            self.assertEqual(len(list(capture.iterdir())), 2)
        self.assertEqual(before, {str(p.relative_to(logs)): p.read_bytes() for p in logs.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
