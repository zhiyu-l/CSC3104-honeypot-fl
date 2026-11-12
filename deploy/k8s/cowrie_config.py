"""Render/validate locally, or run explicitly selected kubectl operations with context checks."""

import argparse
import hashlib
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
CLUSTERS = {org: f"{org}-honeypot-cluster" for org in ORGS}
NAMESPACE = "default"
GROUPS = ("storage", "configuration", "workloads", "services")
EXTERNAL_SECRETS = {"cowrie-host-keys", "cowrie-userdb"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def build(org):
    require(org in CLUSTERS, "Select orga, orgb or orgc")
    result = subprocess.run(
        ["kubectl", "--context", CLUSTERS[org], "--namespace", NAMESPACE,
         "kustomize", str(OVERLAY / org)], check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return list(yaml.safe_load_all(result.stdout))


def validate(docs, org, allow_placeholders=False, schema_path=None):
    require(org in CLUSTERS, "Select orga, orgb or orgc")
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
        require(doc["kind"] not in {"Namespace", "Secret"}, "No Namespace or Secret objects belong in rendered workloads")
        require(doc["metadata"].get("labels", {}).get("organisation") == org, "Mixed organisation resources")
        require(doc["metadata"].get("namespace", "") == ("" if doc["kind"] == "PersistentVolume" else NAMESPACE),
                "All namespaced resources must use default")
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

    deployment = resource("Deployment", NAMESPACE, "cowrie")
    pod_template = deployment["spec"]["template"]
    labels = pod_template["metadata"]["labels"]
    require(labels["organisation"] == org, "Organisation label mismatch")
    for key, value in deployment["spec"]["selector"]["matchLabels"].items():
        require(labels.get(key) == value, "Deployment selector mismatch")
    check_pod(pod_template["spec"], NAMESPACE)
    hpa = resource("HorizontalPodAutoscaler", NAMESPACE, "cowrie-hpa")["spec"]
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
    pvc = resource("PersistentVolumeClaim", NAMESPACE, "cowrie-logs")["spec"]
    pv = resource("PersistentVolume", "", pvc["volumeName"])["spec"]
    require(pv["claimRef"] == {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": NAMESPACE, "name": "cowrie-logs"}, "PV reservation mismatch")
    require(pv["storageClassName"] == pvc["storageClassName"] == "", "Static storage class mismatch")
    require(pv["accessModes"] == pvc["accessModes"] == ["ReadWriteMany"], "EFS access mode mismatch")
    require(pv["capacity"]["storage"] == pvc["resources"]["requests"]["storage"] == "5Gi", "Storage capacity mismatch")
    require(pv["persistentVolumeReclaimPolicy"] == "Retain", "EFS must be retained")
    require(pv["csi"]["driver"] == "efs.csi.aws.com" and "tls" in pv["mountOptions"], "EFS CSI configuration mismatch")
    cron = resource("CronJob", NAMESPACE, "cowrie-logs-to-s3")["spec"]
    require(type(cron["suspend"]) is bool and cron["concurrencyPolicy"] == "Forbid", "CronJob safety settings changed")
    require(cron["schedule"] == "0 0 * * *" and cron["timeZone"] == "Asia/Singapore", "CronJob schedule changed")
    upload_pod = cron["jobTemplate"]["spec"]["template"]["spec"]
    check_pod(upload_pod, NAMESPACE)
    uploader = next(c for c in upload_pod["containers"] if c["name"] == "uploader")
    require(upload_pod["serviceAccountName"] == "cowrie-log-uploader", "Nightly processor must use Pod Identity")
    security = upload_pod["securityContext"]
    require(security["runAsNonRoot"] is True and security["runAsUser"] > 0 and
            security["runAsGroup"] > 0 and security["seccompProfile"]["type"] == "RuntimeDefault",
            "Nightly processor must run non-root with RuntimeDefault seccomp")
    state_prepare = next(c for c in upload_pod["initContainers"] if c["name"] == "prepare-processor-state")
    require(state_prepare["image"] == uploader["image"], "Nightly images differ")
    require(uploader["command"] == ["python", "/app/services/log-processor/nightly.py"],
            "AWS must run the combined nightly processor")
    for container in (uploader, state_prepare):
        require(not container.get("envFrom"), "AWS nightly containers must not use static credential Secrets")
        sc = container["securityContext"]
        require(sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and
                sc["capabilities"]["drop"] == ["ALL"], "Nightly container security changed")
    state_mount = next(v for v in uploader["volumeMounts"] if v["mountPath"] == "/state")
    require(state_mount == {"name": "logs", "mountPath": "/state", "subPath": ".nightly-state"},
            "Checkpoints must persist in the dedicated EFS state subdirectory")
    require(cron["jobTemplate"]["spec"]["template"]["metadata"]["labels"]["organisation"] == org,
            "Uploader pod organisation label mismatch")
    org_env = next(e for e in uploader["env"] if e["name"] == "ORG")
    require(org_env.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") ==
            "metadata.labels['organisation']", "S3 organisation must come from the pod label, not namespace")
    upload_mount = next(v for v in uploader["volumeMounts"] if v["mountPath"] == "/logs")
    require(upload_mount.get("readOnly") is True and
            not any(k in upload_mount for k in ("subPath", "subPathExpr")),
            "Uploader must see the full EFS root, including deleted pods' directories")
    upload_volume = next(v for v in upload_pod["volumes"] if v["name"] == upload_mount["name"])
    require(upload_volume.get("persistentVolumeClaim", {}).get("claimName") ==
            log_volume["persistentVolumeClaim"]["claimName"],
            "Cowrie and uploader must share the same persistent claim")
    service = resource("Service", NAMESPACE, "cowrie-ssh")["spec"]
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
    require(len(docs) == 11, "Expected exactly 11 resources for one cluster")
    print(f"{org} / {CLUSTERS[org]} / default: validated {len(docs)} resources; YAML, uniqueness, references, selectors, storage, HPA and CronJob settings pass.")
    print("External dependency: cowrie-host-keys and cowrie-userdb Secrets in this cluster's default namespace.")


def load_settings(settings_path):
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    require("REPLACE_" not in json.dumps(settings), "Replace every placeholder in local settings")
    require(re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", settings["aws_region"]), "Invalid AWS region")
    require("s3_bucket" not in settings, "Move s3_bucket into each organisation's settings")
    require(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", settings["nightly_image"]),
            "nightly_image must be pinned to a sha256 digest")
    require(re.fullmatch(r"fs-[0-9a-f]{8,17}", settings["efs_filesystem_id"]), "Invalid EFS ID")
    require(set(settings["organisations"]) == set(ORGS), "Configure exactly orga, orgb and orgc")
    access_points, roles = set(), set()
    for org, values in settings["organisations"].items():
        require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", values["s3_bucket"]), "Invalid bucket name")
        require(type(values.get("schedule_enabled", False)) is bool, "schedule_enabled must be a boolean")
        for field in ("uploader_uid", "uploader_gid"):
            require(type(values.get(field, 999)) is int and values.get(field, 999) > 0,
                    f"{field} must be a positive non-root integer")
        require(values["cluster_name"] == values["kubectl_context"] == CLUSTERS[org],
                f"{org} must use cluster and context {CLUSTERS[org]}")
        arn = re.fullmatch(r"arn:(aws(?:-us-gov|-cn)?):eks:([^:]+):(\d{12}):cluster/(.+)", values["cluster_arn"])
        require(arn and arn[2] == settings["aws_region"] and arn[4] == CLUSTERS[org],
                f"Cluster ARN does not identify {CLUSTERS[org]} in the configured region")
        kms = values.get("kms_key_arn")
        if kms is not None:
            require(re.fullmatch(rf"arn:{arn[1]}:kms:{arn[2]}:{arn[3]}:key/[a-zA-Z0-9-]+", kms),
                    "KMS key must be an exact key ARN in the cluster account and region")
        role = re.fullmatch(r"arn:(aws(?:-us-gov|-cn)?):iam::(\d{12}):role/[\w+=,.@/-]+", values["uploader_role_arn"])
        require(role and (role[1], role[2]) == (arn[1], arn[3]),
                f"Uploader role must belong to {org}'s cluster account/partition")
        require(values["uploader_role_arn"] not in roles, "Use a distinct uploader IAM role per organisation")
        roles.add(values["uploader_role_arn"])
        ap = values["efs_access_point_id"]
        require(re.fullmatch(r"fsap-[0-9a-f]{17}", ap), f"Invalid access point for {org}")
        require(ap not in access_points, "Organisations must use distinct EFS access points")
        access_points.add(ap)
        require(values["allowed_source_cidrs"], f"No allowed sources for {org}")
        for cidr in values["allowed_source_cidrs"]:
            network = ipaddress.ip_network(cidr, strict=True)
            require(network.version == 4 and network.prefixlen > 0, "Use restricted IPv4 CIDRs")
    return settings


def target_for(settings, org):
    values = settings["organisations"][org]
    return {"organisation": org, "cluster_name": values["cluster_name"],
            "cluster_arn": values["cluster_arn"], "kubectl_context": values["kubectl_context"],
            "namespace": NAMESPACE,
            "settings_sha256": hashlib.sha256(json.dumps(
                {"aws_region": settings["aws_region"], "nightly_image": settings["nightly_image"],
                 "efs_filesystem_id": settings["efs_filesystem_id"], "organisation": values},
                sort_keys=True).encode()).hexdigest()}


def render(settings_path, organisations):
    settings = load_settings(settings_path)
    outputs = []
    for org in organisations:
        docs = build(org)
        values = settings["organisations"][org]
        for doc in docs:
            if doc["kind"] == "PersistentVolume":
                doc["spec"]["csi"]["volumeHandle"] = settings["efs_filesystem_id"] + "::" + values["efs_access_point_id"]
            elif doc["kind"] == "Service":
                doc["spec"]["loadBalancerSourceRanges"] = values["allowed_source_cidrs"]
            elif doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "cowrie-upload-settings":
                doc["data"] = {"AWS_DEFAULT_REGION": settings["aws_region"], "S3_BUCKET": values["s3_bucket"]}
            elif doc["kind"] == "CronJob":
                doc["spec"]["suspend"] = not values.get("schedule_enabled", False)
                pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                pod["securityContext"].update(runAsUser=values.get("uploader_uid", 999),
                                              runAsGroup=values.get("uploader_gid", 999))
                for container in pod["containers"] + pod["initContainers"]:
                    container["image"] = settings["nightly_image"]
        validate(docs, org)
        outputs.append((org, docs))
    # Validate every selected cluster before writing any outputs. Never combine
    # default-namespace resources from distinct clusters into one applicable file.
    for org, docs in outputs:
        output = LOCAL / "rendered" / org
        output.mkdir(parents=True, exist_ok=True)
        for group in GROUPS:
            kinds = {"storage": {"PersistentVolume", "PersistentVolumeClaim"},
                     "configuration": {"ConfigMap", "ServiceAccount"},
                     "workloads": {"Deployment", "HorizontalPodAutoscaler", "CronJob"},
                     "services": {"Service"}}[group]
            (output / f"{group}.yaml").write_text(
                yaml.safe_dump_all([d for d in docs if d["kind"] in kinds], sort_keys=False), encoding="utf-8")
        (output / "target.json").write_text(json.dumps(target_for(settings, org), indent=2) + "\n", encoding="utf-8")
        # Focused upgrade artifact: do not reconcile Cowrie, HPA, storage or NLB
        # when installing/updating only the nightly pipeline on existing clusters.
        nightly = [d for d in docs if d["kind"] == "CronJob" or
                   (d["kind"], d["metadata"]["name"]) in {
                       ("ConfigMap", "cowrie-upload-settings"),
                       ("ServiceAccount", "cowrie-log-uploader")}]
        (output / "nightly.yaml").write_text(yaml.safe_dump_all(nightly, sort_keys=False), encoding="utf-8")
        render_identity(settings, org, output / "infra")
        print(f"Rendered {org} for context {CLUSTERS[org]} to {output}; no deployment performed.")


def render_identity(settings, org, output):
    values = settings["organisations"][org]
    output.mkdir(exist_ok=True)
    inputs = json.loads((ROOT / f"infra/cowrie/{org}/pod-identity-association.template.json").read_text())
    inputs["roleArn"] = values["uploader_role_arn"]
    require(inputs["clusterName"] == CLUSTERS[org] and inputs["namespace"] == NAMESPACE,
            "Pod Identity association target mismatch")
    trust = json.loads((ROOT / "infra/cowrie/pod-identity-trust.template.json").read_text())
    conditions = trust["Statement"][0]["Condition"]["StringEquals"]
    conditions["aws:RequestTag/eks-cluster-arn"] = values["cluster_arn"]
    require(conditions["aws:RequestTag/kubernetes-namespace"] == NAMESPACE, "Pod Identity trust must use default")
    policy = json.loads((ROOT / "infra/cowrie/uploader-policy.template.json").read_text())
    partition = values["cluster_arn"].split(":")[1]
    bucket = values["s3_bucket"]
    bucket_arn = f"arn:{partition}:s3:::{bucket}"
    policy["Statement"][0]["Resource"] = f"{bucket_arn}/{org}/raw/*"
    policy["Statement"][1]["Resource"] = f"{bucket_arn}/{org}/processed/sessions/*"
    reader = {"Version": "2012-10-17", "Statement": [
        {"Sid": "ListOrganisationLogs", "Effect": "Allow", "Action": ["s3:ListBucket"],
         "Resource": bucket_arn, "Condition": {"StringLike": {"s3:prefix": [
             f"{org}/raw/*", f"{org}/processed/sessions/*"]}}},
        {"Sid": "ReadOrganisationLogs", "Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": [f"{bucket_arn}/{org}/raw/*", f"{bucket_arn}/{org}/processed/sessions/*"]},
    ]}
    if values.get("kms_key_arn"):
        policy["Statement"].append({"Sid": "KmsForUpload", "Effect": "Allow",
            "Action": ["kms:GenerateDataKey", "kms:Decrypt"], "Resource": values["kms_key_arn"]})
        reader["Statement"].append({"Sid": "KmsForRead", "Effect": "Allow",
            "Action": ["kms:Decrypt"], "Resource": values["kms_key_arn"]})
    for name, content in (("pod-identity-association", inputs), ("pod-identity-trust", trust),
                          ("uploader-policy", policy), ("reader-policy", reader)):
        require("REPLACE_" not in json.dumps(content), "Unresolved IAM input placeholders")
        (output / f"{name}.json").write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")


def check_context(settings, org):
    """Read only the selected local kubeconfig cluster reference, never credentials."""
    result = subprocess.run(
        ["kubectl", "--context", CLUSTERS[org], "--namespace", NAMESPACE,
         "config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.cluster}"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    require(result.stdout.strip() == settings["organisations"][org]["cluster_arn"],
            f"Context {CLUSTERS[org]} does not refer to the configured EKS cluster ARN; refusing operation")


def check_file_target(org, filename, settings):
    path = Path(filename).resolve()
    rendered = (LOCAL / "rendered" / org).resolve()
    secrets = (LOCAL / "secrets" / org).resolve()
    is_rendered = path.parent == rendered and path.name in ({f"{g}.yaml" for g in GROUPS} | {"nightly.yaml"})
    is_secret = path.parent == secrets and path.name.endswith(".secret.yaml")
    require(is_rendered or is_secret, "Use only the selected organisation's rendered YAML or local Secret files")
    if is_rendered:
        target = json.loads((rendered / "target.json").read_text(encoding="utf-8"))
        require(target == target_for(settings, org), "Rendered target is stale or belongs to another cluster; render again")
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8-sig")):
        require(isinstance(doc, dict) and doc["kind"] != "Namespace", "No Namespace operations are permitted")
        require(doc["metadata"].get("namespace", "") == ("" if doc["kind"] == "PersistentVolume" else NAMESPACE),
                "File targets a namespace other than default")
        if is_secret:
            require(doc["kind"] == "Secret" and doc["metadata"]["name"] in EXTERNAL_SECRETS,
                    "Unexpected local Secret")
        else:
            require(doc["metadata"].get("labels", {}).get("organisation") == org,
                    "File contains another organisation's resources")


def run_kubectl(settings, org, arguments):
    arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
    require(arguments and not arguments[0].startswith("-"), "Supply a kubectl command after --")
    require(arguments[0] not in {"config", "kustomize"}, "Use check-context or validate for local configuration")
    top = arguments[:arguments.index("--")] if "--" in arguments else arguments
    blocked = {"--context", "--namespace", "--kubeconfig", "--cluster", "--user", "--server",
               "--token", "--all-namespaces", "-A", "-k", "--kustomize"}
    for i, token in enumerate(top):
        flag = token.split("=", 1)[0]
        require(flag not in blocked and not token.startswith("-n") and not token.startswith("-k"),
                "Context, namespace and kubeconfig/cluster overrides are forbidden")
        filename = None
        if token in {"-f", "--filename"}:
            require(i + 1 < len(top), "Missing filename")
            filename = top[i + 1]
        elif token.startswith("--filename="):
            filename = token.split("=", 1)[1]
        elif token.startswith("-f") and len(token) > 2:
            filename = token[2:]
        if filename is not None:
            check_file_target(org, filename, settings)
    check_context(settings, org)
    # The wrapper never changes current-context and never relies on its namespace.
    return subprocess.run(["kubectl", "--context", CLUSTERS[org], "--namespace", NAMESPACE, *arguments]).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "render", "check-context", "kubectl"):
        sub = commands.add_parser(command)
        sub.add_argument("--org", choices=ORGS, required=command in {"check-context", "kubectl"})
        if command != "validate":
            sub.add_argument("--settings", type=Path, default=LOCAL / "settings.json")
        if command == "validate":
            sub.add_argument("--rendered", action="store_true")
            sub.add_argument("--schema", type=Path, help="Official Kubernetes swagger.json for offline checks")
        if command == "kubectl":
            sub.add_argument("kubectl_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    organisations = (args.org,) if args.org else ORGS
    if args.command == "render":
        render(args.settings, organisations)
    elif args.command == "validate":
        for org in organisations:
            docs = ([d for group in GROUPS for d in yaml.safe_load_all(
                (LOCAL / "rendered" / org / f"{group}.yaml").read_text(encoding="utf-8"))]
                if args.rendered else build(org))
            validate(docs, org, allow_placeholders=not args.rendered, schema_path=args.schema)
    else:
        settings = load_settings(args.settings)
        if args.command == "check-context":
            check_context(settings, args.org)
            print(f"Verified context {CLUSTERS[args.org]} -> configured EKS ARN; namespace default")
        else:
            raise SystemExit(run_kubectl(settings, args.org, args.kubectl_args))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
