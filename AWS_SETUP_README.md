# CSC3104 Honeypot FL: AWS Infrastructure and Security

This README documents the AWS infrastructure built for the CSC3104 federated-learning honeypot project: what was built, how it is configured, and why each security choice was made.

- **Scope:** AWS infrastructure and security only (network, encryption, TLS/mTLS material, static IPs, IAM, isolation). Application code, container images and Kubernetes workloads belong to the app, Cowrie and FL teams.
- **Status (19 Nov 2026):** coordinator, orga, orgb and orgc built and checked. **FL clients reach the coordinator through AWS PrivateLink** (internal NLB + endpoint service); the public NLB with static IPs is kept only as a fallback.
- **Account / region:** `909891185696`, `ap-southeast-1` (Singapore), AZs `ap-southeast-1a` and `ap-southeast-1b`.
- **Tooling:** AWS CLI in PowerShell (AWS CloudShell, `pwsh`). All IDs are saved in CloudShell under `/home/cloudshell-user/csc3104-aws/` (`progress.json` for the coordinator, `orga.json` / `orgb.json` / `orgc.json` for the orgs). Commands are in `coordinator-commands.ps1`; orgb and orgc were built with `org-setup.ps1 -Org orgb|orgc`.

---

## 1. Architecture

```
                     ┌──────────── Org VPC (orga 10.0/16, orgb 10.1/16, orgc 10.2/16) ───────────┐
 Attackers ──► Cowrie NLB ──► Cowrie pods (EC2 nodes, honeypot subnets, namespace default)          │
                     │            │ logs (EFS)                                                      │
                     │            ▼                                                                 │
                     │      CronJob ──► S3 <org>/raw/ ──► processing ──► <org>/processed/           │
                     │                                                        │                     │
                     │      FL client pod (Fargate, FL subnets, namespace fl) ◄┘                     │
                     │            │ TLS to coordinator.fl.internal:9092                             │
                     │            ▼                                                                 │
                     │      PrivateLink interface endpoint (ENI in the FL subnet)                   │
                     └────────────┼─────────────────────────────────────────────────────────────────┘
                                  │  AWS network only, never the internet
                                  ▼
                     ┌──────────── Coordinator VPC (10.100.0.0/16) ─────────────┐
                     │  Endpoint service csc3104-coord-privatelink               │
                     │        ▼                                                  │
                     │  Internal NLB csc3104-coord-nlb-int (TCP 9092)            │
                     │        ▼                                                  │
                     │  ECS Fargate "superlink" (private subnets, port 8000)     │
                     │    ├─ EFS /models (checkpoints)                           │
                     │    ├─ Secrets Manager (TLS)                               │
                     │    └─ S3 final model                                      │
                     │  VPC endpoints only: no internet route                    │
                     │                                                           │
                     │  Public NLB (2 Elastic IPs): fallback only, no targets    │
                     └───────────────────────────────────────────────────────────┘
```

Raw honeypot data never leaves its org. Only model weights travel to the coordinator, and they travel over PrivateLink, so FL traffic never crosses the public internet.

---

## 2. Naming and network plan

| Item | Coordinator | orga | orgb | orgc |
|---|---|---|---|---|
| VPC CIDR | 10.100.0.0/16 | 10.0.0.0/16 | 10.1.0.0/16 | 10.2.0.0/16 |
| Public subnets | .0.0/24, .1.0/24 (NLB) | .0.0/24, .1.0/24 (NAT, Cowrie NLB) | same pattern | same pattern |
| Private subnets | .10.0/24, .11.0/24 (task, EFS, endpoints) | honeypot .10.0/24, .11.0/24 | same | same |
| FL subnets | n/a | .20.0/24, .21.0/24 (Fargate) | same | same |

Non-overlapping CIDRs keep security group rules simple and leave room for peering later. PrivateLink would work even with overlap.

| Setting | Value |
|---|---|
| Name prefix | `csc3104` |
| NLB port (clients dial) → container port | 9092 → 8000 (repo's SuperLink port) |
| Coordinator hostname in TLS cert | `coordinator.fl.internal` |
| Client → coordinator path | **AWS PrivateLink** (endpoint service `csc3104-coord-privatelink` → internal NLB `csc3104-coord-nlb-int`) |
| `coordinator.fl.internal` as seen by each FL client | orga `10.0.20.100`, orgb `10.1.20.25`, orgc `10.2.20.80` (each org's own PrivateLink endpoint in its FL subnet), pinned with `hostAliases` in `deploy/k8s/overlays/aws/fl/sg-orgX/tls.patch.yaml` |
| Coordinator static IPs (public fallback NLB only) | `52.77.203.131`, `52.221.118.229` |
| Org static outbound IPs (NAT) | orga `52.74.24.242`, orgb `18.140.111.127`, orgc `18.140.29.66` |

---

## 3. Coordinator

| Component | Configuration | Justification |
|---|---|---|
| VPC `csc3104-coord-vpc` (`vpc-0756c784574403caf`) | 2 public subnets (NLB only), 2 private subnets; private route table has **no internet route** | The coordinator has no route to the internet, so a compromised task cannot exfiltrate data or download tools. |
| Security groups | NLB → task (8000) only; task → endpoints (443), S3 prefix list (443), EFS (2049) only; default allow-all egress removed | Least privilege at the network layer: each component only reachable from the one that needs it. |
| VPC endpoints | Interface: `ecr.api`, `ecr.dkr`, `logs`, `secretsmanager`, `ssmmessages`; Gateway: S3 | Image pulls, logs, secrets and ECS Exec without internet access. Traffic stays on the AWS network. |
| KMS key `alias/csc3104-fl` | Customer-managed, rotation on; CloudWatch Logs allowed by key policy | One key we control and can audit (CloudTrail) encrypts EFS, S3, Secrets Manager, ECR and logs. |
| Certificate authority | Own CA (`CN=CSC3104 FL Root CA`, EC P-256, 1 year). **CA private key kept offline** (downloaded, deleted from CloudShell) | AWS Private CA costs about $400/month. An offline root means a cloud compromise cannot mint new certificates. |
| Server certificate | `CN/SAN=coordinator.fl.internal`, serverAuth, 90 days | Clients verify they are talking to the real coordinator. Short lifetime limits damage if leaked. |
| Secret `csc3104/coordinator/tls` | `ca_crt`, `server_crt`, `server_key`, KMS-encrypted; injected into the task as env vars | TLS material is never baked into the image or stored in Git. |
| EFS `csc3104-coord-models` | KMS-encrypted, elastic throughput, mount targets in both AZs, access point `/models` (UID/GID 1000, 750) | Checkpoints survive a task crash or AZ move. The access point forces the non-root `app` user and directory. |
| EFS file system policy | `DenyNonTLS` + only the task role, only through the access point | EFS itself refuses unencrypted or other-identity mounts. |
| S3 `csc3104-fl-models-909891185696` | Public access blocked, versioning, SSE-KMS with bucket key, bucket policy `DenyNonTLS` | Final model for evaluation and the report. Versioning protects against overwrite. HTTPS only. |
| ECR `csc3104/coordinator` | Immutable tags, scan on push, KMS | An image tag cannot be silently replaced; vulnerabilities are reported on push. |
| Log group `/ecs/csc3104-coordinator` | 30-day retention, KMS | Encrypted logs, bounded storage cost. |
| IAM execution role | ECS task execution policy + read TLS secret + KMS decrypt | Used only by ECS to start the task. |
| IAM task role | EFS mount/write via access point only; S3 PutObject to models bucket; KMS for S3; ECS Exec | The coordinator can do exactly what it needs and nothing else. |
| **Internal NLB + PrivateLink (primary path)** | `csc3104-coord-nlb-int` in the private subnets, **TCP listener 9092 (TLS passthrough)** → task 8000. SG allows 9092 only from the 3 org CIDRs (10.0/16, 10.1/16, 10.2/16). Endpoint service `csc3104-coord-privatelink`, **acceptance required**, allowed principal = this account | **This is how clients connect.** Each org gets a private interface endpoint inside its own VPC, so FL traffic stays on the AWS network and never touches the internet. The coordinator is never exposed publicly, needs no inbound internet route, and only endpoint connections we accept can reach it. Scales to more orgs by adding endpoints, with no VPC peering, route changes or CIDR coordination. |
| Public NLB `csc3104-coord-nlb` (fallback) | 2 Elastic IPs, internet-facing, cross-zone on, NLB security group, TCP listener 9092 (TLS passthrough), idle timeout 600 s. **Not attached to the ECS service** (no targets) | Kept as a static-IP fallback for a client that cannot use PrivateLink. Unused in the current deployment; delete it to remove cost and an exposed entry point. |
| Public NLB allowlist | Inbound 9092 only from the org NAT IPs (`52.74.24.242/32`, `18.140.111.127/32`, `18.140.29.66/32`) | Only relevant if the fallback is used. Defence in depth on top of TLS. Identity still comes from keys and certificates, not IPs. |
| ECS cluster / task definition | `csc3104-coordinator-cluster`, task definition `csc3104-coordinator` (rev. 6 at time of writing): Fargate, container `superlink`, port 8000, `FL_MODEL_DIR=/models`, TLS secrets and `FL_SUPERNODE_PUBLIC_KEYS` injected as env vars, EFS with TLS + IAM | Serverless, no hosts to patch. Secrets never baked into the image. |
| ECS service `csc3104-coordinator-service` | Running (created by the coordinator team). Desired 1, private subnets, no public IP, **registered only with the internal NLB target group** | Only reachable through PrivateLink. Never two coordinators at once (they would both write checkpoints). |

---

## 4. Organisation A (orga)

| Component | Configuration | Justification |
|---|---|---|
| VPC `csc3104-orga-vpc` (`vpc-01083dee77f7e14ec`) | Public, honeypot and FL subnets in 2 AZs, tagged for load balancers | Honeypot and FL client live in separate subnets. |
| NAT gateway | 1 NAT in public-a with Elastic IP `52.74.24.242` | Gives orga one fixed outbound IP for the coordinator allowlist. |
| S3 gateway endpoint | On the private route table | Uploader, log processor and FL client reach S3 without the internet. |
| KMS key `alias/csc3104-orga` | Rotation on | Separate key per org: one org's key cannot decrypt another's data. |
| S3 `csc3104-cowrie-orga-909891185696` | Public access blocked, versioning, SSE-KMS (orga key), `DenyNonTLS`. Prefixes `orga/raw/` and `orga/processed/` | Own-org bucket. Account ID added because `csc3104-cowrie-orga` was already taken. |
| Cowrie EFS | KMS-encrypted, mount targets in honeypot subnets, access point `/orga` (UID/GID 999, 750, same as the repo), `DenyNonTLS`, NFS only from Cowrie nodes | Persistent honeypot logs. EFS is VPC-bound, so each org has its own (not shared as the old repo design assumed). |
| EKS `orga-honeypot-cluster` | Kubernetes 1.37, control plane in honeypot + FL subnets, private and public API endpoint, **secrets envelope-encrypted with the orga KMS key**, api/audit/authenticator logs | Kubernetes Secrets (including the TLS key) are encrypted at rest; audit trail of every API call. |
| Cowrie node security group | Ingress only from the cluster SG and itself; egress only 443, node-to-node and NFS to EFS (default egress removed) | Deny-by-default egress. Attackers cannot use the nodes to scan or attack on other ports. |
| Cowrie node group `orga-cowrie-ng` | Managed, AL2023, **t3.small × 2** (min 1, max 3), honeypot subnets, label `role=honeypot`, launch template with **IMDSv2 required, hop limit 1**, encrypted gp3 disk, no SSH (SSM instead) | A compromised Cowrie container cannot reach the node's metadata service, so it cannot steal the node's AWS credentials. 2 nodes because one t3.small fits only 11 pods. |
| Fargate profile `orga-fl-profile` | Namespace `fl`, FL subnets | The FL client runs in its own microVM, in its own subnets, never on the honeypot nodes. |
| Add-ons | `vpc-cni` (**network policy on**), `kube-proxy`, `coredns`, `eks-pod-identity-agent`, `aws-efs-csi-driver`, `metrics-server` | Network policy enforcement, credentials without static keys, EFS mounting, autoscaling for Cowrie. |
| Pod Security | Namespaces `default` and `fl`: enforce `baseline`, warn `restricted` | Blocks privileged containers and host access; warns on anything not fully hardened. |
| NetworkPolicies (`default`) | `default-deny-all`; `allow-dns`; `cowrie-allow-ssh-in` (TCP 2222 in only); `jobs-allow-aws-egress` (non-Cowrie pods: Pod Identity agent + HTTPS outside 10.0.0.0/8) | Cowrie pods can receive SSH and resolve DNS, nothing else. A hacked honeypot cannot reach the FL client, the VPC or the internet. Upload jobs can reach AWS only. |
| Uploader role (Pod Identity) | `default/cowrie-log-uploader` → PutObject/AbortMultipartUpload on `orga/raw/*`, PutObject on `orga/processed/sessions/*` + KMS GenerateDataKey. Trust bound to namespace, service account and cluster ARN (matches `infra/cowrie/uploader-policy.template.json`) | Used by the combined nightly CronJob (`cowrie-logs-to-s3`), which backs up raw logs and publishes session batches. Write-only: a compromised job cannot read or delete data. Its checkpoint state is kept on EFS, not S3. |
| Log-processor role (Pod Identity) | `default/log-processor` → read `orga/raw/`, write `orga/processed/` | Created for a separate processor job. **Not used by the current AWS overlay**, since the combined nightly job runs as `cowrie-log-uploader`. Can be deleted. |
| FL client role (IRSA) | `fl/fl-client` → read-only `orga/processed/` + KMS decrypt | The FL client can read only its own org's training features. IRSA because Pod Identity does not support Fargate. |
| AWS Load Balancer Controller | Helm, 1 replica, Pod Identity role, region/VPC set explicitly (needed with IMDS hop limit 1) | Creates Cowrie's public NLB. |
| Client certificate | `CN=orga`, clientAuth only, 90 days, signed by the CA. Stored in Secrets Manager `csc3104/orga/fl-client-tls` (orga KMS key) and Kubernetes Secret `fl/fl-client-tls` | Issued for mutual TLS, but **not used by Flower**: the FL client identifies itself with its SuperNode key pair instead (see next row). |
| FL client TLS and identity (as deployed) | Kubernetes Secrets `fl/fl-superlink-ca` (the CA certificate, used as `--root-certificates`) and `fl/fl-supernode-auth` (the SuperNode private key, `--auth-supernode-private-key`). The coordinator holds the matching public keys in `FL_SUPERNODE_PUBLIC_KEYS` | The client verifies the coordinator's certificate (TLS); the coordinator only accepts the three registered SuperNode keys. Together this gives mutual authentication. |
| PrivateLink interface endpoint | Interface endpoint to `csc3104-coord-privatelink` in the org's FL subnet (orga `10.0.20.100`; orgb `10.1.20.25`, orgc `10.2.20.80`), accepted on the coordinator side | The FL client's only way to the coordinator. Private IP inside the org's own VPC; no internet path involved. |
| Coordinator name → PrivateLink endpoint | The FL client pod maps `coordinator.fl.internal` to the org's endpoint IP with `hostAliases`. The Route 53 private zone `fl.internal` (`Z018117873S4SO6C4FRU`) still holds the original public NLB records, but the pod's host alias takes precedence | The name still matches the server certificate, so TLS verification works unchanged; only the IP behind the name moved to PrivateLink. No certificate change was needed. |

---

orgb and orgc were built with the same configuration using `org-setup.ps1` (their own VPC, KMS key, bucket, EFS, EKS cluster, node group, Fargate profile, IAM roles and client certificate).

---

## 5. Verification (orga 9 Nov 2026; orgb, orgc and PrivateLink afterwards)

| Check | Result |
|---|---|
| NAT IP allowlisted on coordinator NLB | `52.74.24.242/32` |
| S3 | `aws:kms`, versioning `Enabled`, public access block `True ×4` |
| Cowrie EFS | encrypted, 2 mount targets |
| EKS | `ACTIVE 1.37`, secrets encrypted, `api audit authenticator` logging |
| Nodes | 2 Ready, IMDS `required` / hop limit `1` on both |
| Fargate profile | `ACTIVE` |
| Network policy | agent `aws-eks-nodeagent` with `--enable-network-policy=true`; 4 policies |
| Pod Identity | cowrie-log-uploader, log-processor, aws-load-balancer-controller |
| IRSA | `arn:aws:iam::909891185696:role/csc3104-orga-fl-client` |
| TLS secret | `ca.crt, tls.crt, tls.key` |
| LB controller | 1/1 ready |
| Name from an `fl` pod on Fargate | `coordinator.fl.internal` → the org's PrivateLink endpoint IP via `hostAliases` (e.g. orga `10.0.20.100`) |
| ECS service targets | Registered with the internal NLB target group only |
| CA key in CloudShell | `False` |
| orgb, orgc full checklist | All passed, including the live NetworkPolicy test (`DNS_OK`, `INTERNET_BLOCKED`) |

Live NetworkPolicy test for orga (a pod labelled `app=cowrie` should print `DNS_OK` and `INTERNET_BLOCKED`): passed on orgb and orgc; record it for orga if needed as evidence.

PrivateLink checks:

```powershell
# Endpoint connections accepted on the coordinator side (expect 3, State = available)
aws ec2 describe-vpc-endpoint-connections --region ap-southeast-1 `
  --query "VpcEndpointConnections[].{Id:VpcEndpointId,Owner:VpcEndpointOwner,State:VpcEndpointState}" --output table

# Targets the ECS service is registered with (expect only the internal NLB target group)
aws ecs describe-services --cluster csc3104-coordinator-cluster --services csc3104-coordinator-service `
  --region ap-southeast-1 --query "services[0].loadBalancers" --output table
```

---

## 6. Differences from the agreed architecture

| Architecture says | What was built | Reason |
|---|---|---|
| Ideally separate AWS accounts | One account, separate VPCs | Simpler for a student project. PrivateLink and IAM still isolate. |
| Cowrie in a public subnet | Cowrie nodes in private honeypot subnets behind a public NLB | Still open to attackers; nodes have no public IPs. More secure. |
| No SG route from honeypot to FL | Cowrie **pods** fully blocked by NetworkPolicy; Cowrie **nodes** can still reach FL pods on 443 (nothing listens) | Closed fully by adding security groups for the FL Fargate pods (pending). |
| Security groups for Fargate FL pods | Not yet; FL pods use the cluster's default SG | Pending. |
| Coordinator reached over the internet at a static IP | **PrivateLink** is the live path; the static-IP public NLB is kept only as a fallback | Keeps all FL traffic on the AWS network, removes the coordinator's public exposure, and scales to more orgs without peering or allowlist changes. |
| PrivateLink allowed principals = 3 org accounts | This account | Single-account setup. |
| NLB port 9092 | NLB 9092 → container 8000 | The repo's SuperLink listens on 8000. |
| Bucket `raw/`, `processed/` | `orga/raw/`, `orga/processed/` | Matches the repo's existing uploader layout. |

Additions not in the architecture: KMS keys, own CA and certificates, private DNS name, coordinator VPC endpoints, Pod Security labels, EKS secrets encryption and audit logs.

---

## 7. Known limitations (for the report)

- **EKS public API endpoint** is enabled so CloudShell can run `kubectl`. Access still requires AWS credentials. Hardening: restrict the allowed CIDRs, or go private-only with CloudShell in the VPC.
- **The load balancer controller runs on the Cowrie nodes.** A fully compromised node could misuse its permissions. Mitigated by IMDS hop limit 1, NetworkPolicies and Cowrie being an emulated shell. Better: a separate system node group.
- **The Cowrie node SG allows outbound 443 to anywhere** (needed for image pulls and AWS APIs). VPC endpoints for ECR/STS/EKS would allow tightening it.
- **NetworkPolicies do not apply to Fargate pods.** The repo's `fl-client-deny-ingress` policy is therefore not enforced. The FL side relies on Fargate microVM isolation, separate subnets and security groups.
- **The PrivateLink endpoint IPs are hard-coded** in each org's `tls.patch.yaml`. If an endpoint is recreated, its IP changes and the overlay must be updated. More robust: point a Route 53 record (per org VPC) at the endpoint's DNS name and drop the host alias.
- **The nightly CronJob is suspended by default** (`suspend: true` unless `schedule_enabled` is set when rendering). Batches so far came from manually started Jobs.
- **FL pods use CoreDNS on the honeypot nodes** for S3 and other names (the coordinator name is fixed by the host alias). A compromised node could spoof DNS, but TLS certificate verification would reject a fake endpoint.
- **Six teammate IAM users have AdministratorAccess** and are EKS cluster admins. This contradicts least privilege; MFA is required of each user. Alternative: namespace-scoped EKS access entries per team.
- **GuardDuty is unavailable** on this account (`SubscriptionRequiredException`). VPC flow logs are unconfirmed.
- **Mutual authentication, not strict mTLS:** the SuperLink presents its TLS certificate, and clients are identified by Flower SuperNode key pairs (`FL_SUPERNODE_PUBLIC_KEYS`). The per-org client certificates are issued but not used by Flower.
- **Internal NLB security group:** remove any leftover rules for CIDRs outside 10.0/16, 10.1/16, 10.2/16 (e.g. 10.3/16, 10.4/16 from the old Sydney build).
- **The public fallback NLB still exists.** It has no targets, but it costs money and is an exposed address. Delete it if the fallback is not needed.
- **Certificates expire after 90 days** (CA after 1 year). Rotation is manual.

---

## 8. Team handoff

| Team | Where | What they need |
|---|---|---|
| Coordinator | ECR `csc3104/coordinator` → ECS `csc3104-coordinator-cluster` | Push the image (new tag each time). Entrypoint writes `FL_TLS_CA_CERT`, `FL_TLS_SERVER_CERT`, `FL_TLS_SERVER_KEY` to files and starts `flower-superlink` with `--ssl-ca-certfile`, `--ssl-certfile`, `--ssl-keyfile` (not `--insecure`). Listen on 8000, checkpoints to `/models`, gRPC keepalive below 600 s. Then create the ECS service. |
| Cowrie | EKS `orga-honeypot-cluster`, namespace `default` | Bucket `csc3104-cowrie-orga-909891185696`, region `ap-southeast-1`, EFS ID and access point (`orga.json`). Change Service `loadBalancerClass` to `service.k8s.aws/nlb`. The PersistentVolume is cluster-wide, so the infra owner applies it. |
| Nightly processor | ECR (nightly image) → EKS `default` | Combined CronJob `cowrie-logs-to-s3` with `serviceAccountName: cowrie-log-uploader`; writes `<org>/raw/by-pod/` and `<org>/processed/sessions/`; must not be labelled `app: cowrie` (it is labelled `organisation: <org>`). |
| FL client | ECR `csc3104/fl-client` → EKS `fl` (Fargate), overlay `deploy/k8s/overlays/aws/fl/sg-<org>` | `serviceAccountName: fl-client` (IRSA, read-only `<org>/processed/`: base CSV, `sessions/`, `exclude/`); Secrets `fl-superlink-ca` and `fl-supernode-auth`; `SUPERLINK_ADDRESS=coordinator.fl.internal:9092` with TLS, host alias to the org's PrivateLink endpoint; `FL_ORG_ID=org-a/org-b/org-c`. |

Team access: IAM users Mervyn, WeiZhi, Amos, Andrew, JiaLer, ZhiYu (group `csc3104-admins`, sign-in `https://909891185696.signin.aws.amazon.com/console`), with EKS cluster-admin access entries on `orga-honeypot-cluster`.

---

## 9. What is left

Done since the first version: orgb and orgc built; NAT IPs allowlisted; PrivateLink endpoints in all three orgs; ECS coordinator service running behind the internal NLB.

1. Security groups for the FL Fargate pods in each org (closes the honeypot node → FL gap).
2. Clean the internal NLB security group of leftover CIDR rules.
3. Decide whether to keep or delete the public fallback NLB and its Elastic IPs.

---

## 10. Running cost (approx., per day)

| Item | Cost |
|---|---|
| Coordinator VPC endpoints (5 × 2 AZs) | $3.10 |
| Coordinator public + internal NLB | $1.20 |
| Elastic IPs (coordinator 2, orga NAT 1) | $0.36 |
| Coordinator ECS task (once running) | $1.20 |
| orga EKS control plane | $2.40 |
| orga NAT gateway | $1.40 + data |
| orga nodes (2 × t3.small; 1 may be free tier) | $0.65–1.30 |
| orga Cowrie NLB (once deployed) | $0.60 |
| KMS, Secrets Manager, EFS, S3, logs | under $0.50 |
| PrivateLink interface endpoints (orgs) | about $0.30 per endpoint per AZ, plus data |

Roughly **$11–12/day** for the coordinator plus orga; each extra org adds about $5–6/day, so about **$22–25/day** with all three orgs and PrivateLink. Deleting the public fallback NLB and its Elastic IPs saves about $0.70/day. Tear down after the demo.

### Teardown order (when finished)
Delete in reverse dependency order:
1. ECS service, then EKS node groups / Fargate profiles / clusters.
2. The PrivateLink interface endpoints in each org VPC, then the endpoint service, then NLBs and target groups.
3. NAT gateways, then release the Elastic IPs.
4. VPC endpoints, EFS (mount targets first), then subnets, route tables, IGWs and VPCs.
5. Empty and delete the S3 buckets, ECR repos, secrets and log groups.
6. Schedule the KMS keys for deletion.
7. Delete IAM roles, users and the OIDC provider.
