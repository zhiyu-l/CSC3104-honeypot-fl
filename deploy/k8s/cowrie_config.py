"""Build, check and render Cowrie templates locally; never contacts a cluster or AWS."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy/k8s/overlays/aws/cowrie"
LOCAL = ROOT / ".local/cowrie"
ORGS = ("orga", "orgb", "orgc")
GROUPS = ("namespaces", "storage", "configuration", "workloads", "services")
EXTERNAL_SECRETS = {"cowrie-host-keys", "cowrie-userdb"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def build():
    result = subprocess.run(
        ["kubectl", "kustomize", str(OVERLAY)], check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return list(yaml.safe_load_all(result.stdout))


def validate(docs, allow_placeholders=False, schema_path=None):
    index = {}

    def walk(value):
        if isinstance(value, dict):
            forbidden = {"status", "managedFields", "resourceVersion", "uid",
                         "creationTimestamp", "generation", "ownerReferences", "finalizers"}
            require(not forbidden.intersection(value), "Generated metadata remains")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for doc in docs:
        require(isinstance(doc, dict), "Empty or invalid YAML document")
        require(doc["kind"] != "Secret", "Secret objects must be generated locally")
        walk(doc)
        metadata = doc["metadata"]
        key = (doc["kind"], metadata.get("namespace", ""), metadata["name"])
        require(key not in index, f"Duplicate resource: {key}")
        index[key] = doc
        annotations = metadata.get("annotations", {})
        require(not any(k.startswith("pv.kubernetes.io/") or k in {
            "kubectl.kubernetes.io/last-applied-configuration",
            "deployment.kubernetes.io/revision"} for k in annotations), "Stale annotations")
    if not allow_placeholders:
        require("REPLACE_" not in yaml.safe_dump_all(docs), "Unresolved template values")

    def resource(kind, namespace, name):
        key = (kind, namespace, name)
        require(key in index, f"Missing resource: {key}")
        return index[key]

    def check_pod(pod, namespace):
        volumes = {v["name"]: v for v in pod.get("volumes", [])}
        for volume in volumes.values():
            for field, kind, name_key in (("configMap", "ConfigMap", "name"),
                                          ("persistentVolumeClaim", "PersistentVolumeClaim", "claimName")):
                if field in volume:
                    resource(kind, namespace, volume[field][name_key])
            if "secret" in volume:
                require(volume["secret"]["secretName"] in EXTERNAL_SECRETS, "Unexpected Secret reference")
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            for mount in container.get("volumeMounts", []):
                require(mount["name"] in volumes, "Mount references missing volume")
                if "subPath" in mount and "configMap" in volumes[mount["name"]]:
                    cm = resource("ConfigMap", namespace, volumes[mount["name"]]["configMap"]["name"])
                    require(mount["subPath"] in cm["data"], "Missing ConfigMap mount key")
            for env in container.get("env", []):
                ref = env.get("valueFrom", {}).get("configMapKeyRef")
                if ref:
                    cm = resource("ConfigMap", namespace, ref["name"])
                    require(ref["key"] in cm["data"], "Missing ConfigMap environment key")
        if "serviceAccountName" in pod:
            resource("ServiceAccount", namespace, pod["serviceAccountName"])

    for org in ORGS:
        resource("Namespace", "", org)
        deployment = resource("Deployment", org, "cowrie")
        pod_template = deployment["spec"]["template"]
        labels = pod_template["metadata"]["labels"]
        require(labels["organisation"] == org, "Organisation label mismatch")
        for key, value in deployment["spec"]["selector"]["matchLabels"].items():
            require(labels.get(key) == value, "Deployment selector mismatch")
        check_pod(pod_template["spec"], org)
        hpa = resource("HorizontalPodAutoscaler", org, "cowrie-hpa")["spec"]
        require(hpa["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "cowrie"}, "HPA target mismatch")
        require((hpa["minReplicas"], hpa["maxReplicas"]) == (1, 3), "HPA bounds changed")
        require(hpa["behavior"]["scaleDown"] == {
            "stabilizationWindowSeconds": 300, "selectPolicy": "Min",
            "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}],
        }, "Scale-down must stabilize for 300 seconds and remove at most one pod per 60 seconds")
        require(hpa["behavior"]["scaleUp"] == {
            "policies": [{"periodSeconds": 15, "type": "Pods", "value": 4},
                         {"periodSeconds": 15, "type": "Percent", "value": 100}],
            "selectPolicy": "Max", "stabilizationWindowSeconds": 0,
        }, "Existing scale-up settings changed")
        require(hpa["metrics"][0]["resource"]["target"]["averageUtilization"] == 60, "HPA CPU target changed")
        # Pod UIDs remain unique across replica deletion/recreation. Each replica
        # mounts a directory on the persistent claim, not an ephemeral volume.
        pod = pod_template["spec"]
        cowrie = next(c for c in pod["containers"] if c["name"] == "cowrie")
        log_mount = next(v for v in cowrie["volumeMounts"]
                         if v["mountPath"] == "/cowrie/cowrie-git/var/log/cowrie")
        require(log_mount.get("subPathExpr") == "$(POD_UID)" and "subPath" not in log_mount,
                "Each Cowrie replica must use its pod UID as its log directory")
        require(not log_mount.get("readOnly", False), "Cowrie logs must be writable")
        log_volume = next(v for v in pod["volumes"] if v["name"] == log_mount["name"])
        require(log_volume.get("persistentVolumeClaim", {}).get("claimName") == "cowrie-logs",
                "Cowrie logs must use persistent EFS storage")
        prepare = next(c for c in pod["initContainers"] if c["name"] == "prepare-log-directory")
        for container in (cowrie, prepare):
            uid = next(e for e in container["env"] if e["name"] == "POD_UID")
            require(uid.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") == "metadata.uid",
                    "Log directory identity must come from the pod UID")
        require(prepare["command"] == ["/bin/sh", "-ec"] and
                prepare["args"] == ['mkdir -p "/efs/$POD_UID"'],
                "Log preparation must create and preserve pod directories")
        prepare_mount = next(v for v in prepare["volumeMounts"] if v["mountPath"] == "/efs")
        require(prepare_mount["name"] == log_mount["name"] and
                not any(k in prepare_mount for k in ("subPath", "subPathExpr")),
                "Log preparation must mount the same EFS root")
        pvc = resource("PersistentVolumeClaim", org, "cowrie-logs")["spec"]
        pv = resource("PersistentVolume", "", pvc["volumeName"])["spec"]
        require(pv["claimRef"] == {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": org, "name": "cowrie-logs"}, "PV reservation mismatch")
        require(pv["storageClassName"] == pvc["storageClassName"] == "", "Static storage class mismatch")
        require(pv["accessModes"] == pvc["accessModes"] == ["ReadWriteMany"], "EFS access mode mismatch")
        require(pv["capacity"]["storage"] == pvc["resources"]["requests"]["storage"] == "5Gi", "Storage capacity mismatch")
        require(pv["persistentVolumeReclaimPolicy"] == "Retain", "EFS must be retained")
        require(pv["csi"]["driver"] == "efs.csi.aws.com" and "tls" in pv["mountOptions"], "EFS CSI configuration mismatch")
        cron = resource("CronJob", org, "cowrie-logs-to-s3")["spec"]
        require(cron["suspend"] is True and cron["concurrencyPolicy"] == "Forbid", "CronJob safety settings changed")
        require(cron["schedule"] == "0 0 * * *" and cron["timeZone"] == "Asia/Singapore", "CronJob schedule changed")
        upload_pod = cron["jobTemplate"]["spec"]["template"]["spec"]
        check_pod(upload_pod, org)
        uploader = next(c for c in upload_pod["containers"] if c["name"] == "uploader")
        upload_mount = next(v for v in uploader["volumeMounts"] if v["mountPath"] == "/logs")
        require(upload_mount.get("readOnly") is True and
                not any(k in upload_mount for k in ("subPath", "subPathExpr")),
                "Uploader must see the full EFS root, including deleted pods' directories")
        upload_volume = next(v for v in upload_pod["volumes"] if v["name"] == upload_mount["name"])
        require(upload_volume.get("persistentVolumeClaim", {}).get("claimName") ==
                log_volume["persistentVolumeClaim"]["claimName"],
                "Cowrie and uploader must share the same persistent claim")
        service = resource("Service", org, "cowrie-ssh")["spec"]
        require("clusterIP" not in service and all("nodePort" not in p for p in service["ports"]), "Allocated Service addresses remain")
        require(service["loadBalancerSourceRanges"], "Missing source restrictions")
        for key, value in service["selector"].items():
            require(labels.get(key) == value, "Service selector mismatch")
        ports = {p["containerPort"] for c in pod_template["spec"]["containers"] for p in c.get("ports", [])}
        require(all(p["targetPort"] in ports for p in service["ports"]), "Service target port mismatch")
    if schema_path:
        import jsonschema
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))

        def normalize_schema(value):
            # Kubernetes OpenAPI v2 encodes IntOrString as a string with a
            # vendor format. Generic JSON Schema validators need both types.
            if isinstance(value, dict):
                if value.get("format") == "int-or-string" or value.get("x-kubernetes-int-or-string"):
                    value["type"] = ["integer", "string"]
                for child in value.values():
                    normalize_schema(child)
            elif isinstance(value, list):
                for child in value:
                    normalize_schema(child)

        normalize_schema(schema)
        for doc in docs:
            group_version = doc["apiVersion"].replace("/", ".")
            if "." not in group_version:
                group_version = "core." + group_version
            definition = "io.k8s.api." + group_version + "." + doc["kind"]
            validator = jsonschema.Draft4Validator({"$ref": "#/definitions/" + definition,
                                                   "definitions": schema["definitions"]})
            validator.validate(doc)
    print(f"Validated {len(docs)} resources; YAML, uniqueness, references, selectors, storage, HPA and CronJob settings pass.")
    print("External dependency: cowrie-host-keys and cowrie-userdb Secrets in each namespace.")


def render(settings_path):
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    require("REPLACE_" not in json.dumps(settings), "Replace every placeholder in local settings")
    require(re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", settings["aws_region"]), "Invalid AWS region")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", settings["s3_bucket"]), "Invalid bucket name")
    require(re.fullmatch(r"fs-[0-9a-f]{8,17}", settings["efs_filesystem_id"]), "Invalid EFS ID")
    require(set(settings["organisations"]) == set(ORGS), "Configure exactly orga, orgb and orgc")
    access_points = set()
    for org, values in settings["organisations"].items():
        ap = values["efs_access_point_id"]
        require(re.fullmatch(r"fsap-[0-9a-f]{17}", ap), f"Invalid access point for {org}")
        require(ap not in access_points, "Organisations must use distinct EFS access points")
        access_points.add(ap)
        require(re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::\d{12}:role/[\w+=,.@/-]+", values["uploader_role_arn"]), f"Invalid role ARN for {org}")
        require(values["allowed_source_cidrs"], f"No allowed sources for {org}")
        for cidr in values["allowed_source_cidrs"]:
            network = ipaddress.ip_network(cidr, strict=True)
            require(network.version == 4 and network.prefixlen > 0, "Use restricted IPv4 CIDRs")
    docs = build()
    for doc in docs:
        org = doc["metadata"].get("namespace") or doc["metadata"].get("labels", {}).get("organisation")
        if doc["kind"] == "PersistentVolume":
            org = doc["spec"]["claimRef"]["namespace"]
            doc["spec"]["csi"]["volumeHandle"] = settings["efs_filesystem_id"] + "::" + settings["organisations"][org]["efs_access_point_id"]
        elif doc["kind"] == "Service":
            doc["spec"]["loadBalancerSourceRanges"] = settings["organisations"][org]["allowed_source_cidrs"]
        elif doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "cowrie-upload-settings":
            doc["data"] = {"AWS_DEFAULT_REGION": settings["aws_region"], "S3_BUCKET": settings["s3_bucket"]}
    validate(docs)
    output = LOCAL / "rendered"
    output.mkdir(parents=True, exist_ok=True)
    for group in GROUPS:
        kinds = {"namespaces": {"Namespace"}, "storage": {"PersistentVolume", "PersistentVolumeClaim"},
                 "configuration": {"ConfigMap", "ServiceAccount"},
                 "workloads": {"Deployment", "HorizontalPodAutoscaler", "CronJob"}, "services": {"Service"}}[group]
        (output / f"{group}.yaml").write_text(
            yaml.safe_dump_all([d for d in docs if d["kind"] in kinds], sort_keys=False), encoding="utf-8")
    print(f"Rendered to {output}; no deployment performed. Role ARNs are used in external Pod Identity setup, not ServiceAccount annotations.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render"))
    parser.add_argument("--settings", type=Path, default=LOCAL / "settings.json")
    parser.add_argument("--rendered", action="store_true", help="Validate local rendered manifests instead of templates")
    parser.add_argument("--schema", type=Path, help="Optional official Kubernetes swagger.json for offline API schema validation")
    args = parser.parse_args()
    if args.command == "render":
        render(args.settings)
    else:
        docs = ([d for group in GROUPS for d in yaml.safe_load_all(
            (LOCAL / "rendered" / f"{group}.yaml").read_text(encoding="utf-8"))]
            if args.rendered else build())
        validate(docs, allow_placeholders=not args.rendered, schema_path=args.schema)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
