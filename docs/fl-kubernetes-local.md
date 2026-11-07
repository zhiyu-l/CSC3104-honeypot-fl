# FL clients on local kind clusters

Runs the FL clients as Kubernetes pods next to the Cowrie clusters from
[cowrie-local.md](cowrie-local.md), with the coordinator as a plain Docker container.
This mirrors AWS: clients on each org's EKS cluster, coordinator on ECS Fargate.
Design: [ADR 0002](adr/0002-fl-placement-on-kubernetes.md).

| Component | Where | Manifests / config |
| --- | --- | --- |
| Coordinator (SuperLink) | Docker container `superlink` | deploy/local/compose.yaml + compose.kind.yaml |
| Org A client (SuperNode) | kind-orga-honeypot-cluster, namespace `fl` | deploy/k8s/overlays/local/fl/orga |
| Org B client | kind-orgb-honeypot-cluster, namespace `fl` | deploy/k8s/overlays/local/fl/orgb |
| Org C client | kind-orgc-honeypot-cluster, namespace `fl` | deploy/k8s/overlays/local/fl/orgc |

Cowrie stays in `default` and is not touched. Each FL client is one pod with no
Service; it dials out to `superlink:8000`. That works because kind's nodes are
containers on Docker's `kind` network, and `compose.kind.yaml` joins the
coordinator to the same network.

Run everything from the repository root in PowerShell. Needs Docker Desktop, kind,
kubectl, the three org clusters from the Cowrie guide (which also creates the
`kind` network), the Python venv from [deploy/local/README.md](../deploy/local/README.md)
(for `flwr`), and the real-data split in `data/processed/kaggle/split/train/`
(see `setup_README.md`). Stop the full Compose stack first if it is running.

## 1. Start the coordinator

```powershell
docker compose -f deploy/local/compose.yaml -f deploy/local/compose.kind.yaml up --build -d superlink
```

Only `superlink` starts; the Compose SuperNodes are not used here. Checkpoints go
to `models/` on the host as usual.

## 2. Build the client image and load it into the org clusters

```powershell
docker compose -f deploy/local/compose.yaml build supernode-a
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kind load docker-image honeypot-fl-client:latest --name "$org-honeypot-cluster"
}
```

Repeat after changing client code, then
`kubectl --context kind-<org>-honeypot-cluster -n fl rollout restart deployment/fl-client`.

## 3. Deploy one client per organisation

Each org only gets its own CSV, as a ConfigMap (about 180 KB, under the 1 MiB limit).

```powershell
$files = @{ orga = 'orgA.csv'; orgb = 'orgB.csv'; orgc = 'orgC.csv' }
foreach ($org in @('orga', 'orgb', 'orgc')) {
    $context = "kind-$org-honeypot-cluster"
    kubectl --context $context apply -k "deploy/k8s/overlays/local/fl/$org"
    kubectl --context $context -n fl create configmap fl-train-data `
        --from-file="train.csv=data/processed/kaggle/split/train/$($files[$org])" `
        --dry-run=client -o yaml | kubectl --context $context apply -f -
    kubectl --context $context -n fl rollout status deployment/fl-client --timeout=180s
}
```

Check that the clients connected:

```powershell
docker compose -f deploy/local/compose.yaml logs superlink --tail=20
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kubectl --context "kind-$org-honeypot-cluster" -n fl logs deployment/fl-client --tail=5
}
```

If a client log shows it cannot resolve `superlink`, use the container's IP on the
`kind` network instead:

```powershell
$ip = docker inspect -f '{{.NetworkSettings.Networks.kind.IPAddress}}' (docker compose -f deploy/local/compose.yaml ps -q superlink)
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kubectl --context "kind-$org-honeypot-cluster" -n fl set env deployment/fl-client SUPERLINK_ADDRESS="${ip}:8000"
}
```

The IP can change when the container is recreated, so the name is the default.

## 4. Run training

Port 8000 is published to 127.0.0.1, so the `local-deployment` entry in `flwr`'s
`config.toml` from the Docker guide works unchanged:

```powershell
cd deploy/flower-app
flwr run . local-deployment --stream --run-config "fail-prob=0.0 num-server-rounds=5"
```

## 5. Fault-tolerance checks

Kill an org's client during a run; the Deployment replaces it and the coordinator
continues on quorum (2 of 3) or retries the round:

```powershell
kubectl --context kind-orgb-honeypot-cluster -n fl delete pod -l app=fl-client
```

Kill the coordinator during a run. The clients keep retrying and reconnect when
it is back, but the run itself stops, so resume it from the last checkpoint:

```powershell
docker compose -f deploy/local/compose.yaml restart superlink
cd deploy/flower-app
flwr run . local-deployment --stream --run-config "resume-from=/app/models/<run-name>"
```

`<run-name>` is the folder under `models/` printed at the start of the first run.

## Cleanup

```powershell
foreach ($org in @('orga', 'orgb', 'orgc')) {
    kubectl --context "kind-$org-honeypot-cluster" delete namespace fl
}
docker compose -f deploy/local/compose.yaml -f deploy/local/compose.kind.yaml down
```

Deleting the `fl` namespace leaves Cowrie in `default` running.
