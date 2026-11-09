# CSC3104 Honeypot FL: AWS Infrastructure and Security

This README documents the AWS infrastructure built for the CSC3104 federated-learning honeypot project: what was built, how it is configured, and why each security choice was made.

- **Scope:** AWS infrastructure and security only (network, encryption, TLS/mTLS material, static IPs, IAM, isolation). Application code, container images and Kubernetes workloads belong to the app, Cowrie and FL teams.
- **Status (9 Nov 2026):** coordinator and organisation A (orga) built and checked. orgb and orgc not yet built.
- **Account / region:** `909891185696`, `ap-southeast-1` (Singapore), AZs `ap-southeast-1a` and `ap-southeast-1b`.
- **Tooling:** AWS CLI in PowerShell (AWS CloudShell, `pwsh`). All IDs are saved in CloudShell under `/home/cloudshell-user/csc3104-aws/` (`progress.json` for the coordinator, `orga.json` for orga). Commands are in `coordinator-commands.ps1`.

---

## 1. Architecture

```
                     ┌──────────────────── Org VPC (orga 10.0.0.0/16) ─────────────────────┐
 Attackers ──► Cowrie NLB ──► Cowrie pods (EC2 nodes, honeypot subnets, namespace default)    │
                     │            │ logs (EFS)                                                │
                     │            ▼                                                           │
                     │      Upload CronJob ──► S3 orga/raw/ ──► Log processor ──► orga/processed/
                     │                                                             │          │
                     │      FL client pod (Fargate, FL subnets, namespace fl) ◄────┘          │
                     │            │ TLS to coordinator.fl.internal:9092                       │
                     └────────────┼─────── NAT gateway (static IP) ──────────────────────────┘
                                  ▼
                     ┌──────────── Coordinator VPC (10.100.0.0/16) ─────────────┐
                     │  Public NLB (2 Elastic IPs, TCP 9092 passthrough)         │
                     │  Internal NLB + PrivateLink endpoint service (optional)   │
                     │        ▼                                                  │
                     │  ECS Fargate "superlink" (private subnets, port 8000)     │
                     │    ├─ EFS /models (checkpoints)                           │
                     │    ├─ Secrets Manager (TLS)                               │
                     │    └─ S3 final model                                      │
                     │  VPC endpoints only: no internet route                    │
                     └───────────────────────────────────────────────────────────┘
```

Raw honeypot data never leaves its org. Only model weights travel to the coordinator.

---

## 2. Naming and network plan

| Item | Coordinator | orga | orgb (planned) | orgc (planned) |
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
| Coordinator static IPs | `52.77.203.131`, `52.221.118.229` |
| orga static outbound IP (NAT) | `52.74.24.242` |

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
| Public NLB `csc3104-coord-nlb` | 2 Elastic IPs, internet-facing, cross-zone on, NLB security group, **TCP listener 9092 (TLS passthrough)**, idle timeout 600 s | Static address that never changes when the task is replaced. Passthrough keeps TLS end to end so the coordinator sees the client identity. Longer idle timeout so connections idle during training are not dropped. |
| NLB allowlist | Inbound 9092 only from org NAT IPs (orga `52.74.24.242/32` so far) | Defence in depth on top of TLS. Identity still comes from certificates, not IPs. |
| Internal NLB + PrivateLink | `csc3104-coord-nlb-int` (private subnets, SG allows 9092 only from the 3 org CIDRs), endpoint service `csc3104-coord-privatelink`, acceptance required, allowed principal = this account | Private path for orgs that must not use the internet. Same coordinator task behind both NLBs. |
| ECS cluster / task definition | `csc3104-coordinator-cluster`, `csc3104-coordinator:1`: Fargate, 1 vCPU / 2 GB, container `superlink`, port 8000, image `…/csc3104/coordinator:v1`, `FL_MODEL_DIR=/models`, EFS with TLS + IAM | Serverless, no hosts to patch. |
| ECS service (pending image) | Desired 1, both private subnets, no public IP, **min 0% / max 100%**, circuit breaker with rollback, ECS Exec on | Never two coordinators at once (they would both write checkpoints). Replacement task can start in either AZ. Shell access without SSH or open ports. |

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
| Uploader role (Pod Identity) | `default/cowrie-log-uploader` → PutObject/AbortMultipartUpload on `orga/raw/*` + KMS GenerateDataKey. Trust bound to namespace, service account and cluster ARN (same as the repo template) | Write-only. A compromised uploader cannot read or delete data. |
| Log-processor role (Pod Identity) | `default/log-processor` → read `orga/raw/`, write `orga/processed/` | Only the access it needs. |
| FL client role (IRSA) | `fl/fl-client` → read-only `orga/processed/` + KMS decrypt | The FL client can read only its own org's training features. IRSA because Pod Identity does not support Fargate. |
| AWS Load Balancer Controller | Helm, 1 replica, Pod Identity role, region/VPC set explicitly (needed with IMDS hop limit 1) | Creates Cowrie's public NLB. |
| Client certificate | `CN=orga`, clientAuth only, 90 days, signed by the CA. Stored in Secrets Manager `csc3104/orga/fl-client-tls` (orga KMS key) and Kubernetes Secret `fl/fl-client-tls` (`ca.crt`, `tls.crt`, `tls.key`) | Client half of mutual TLS. Private key never left AWS's encrypted stores; local copy deleted. |
| Private DNS `fl.internal` (`Z018117873S4SO6C4FRU`) | Associated with orga VPC only. `coordinator.fl.internal` → `52.77.203.131`, `52.221.118.229` | The name matches the server certificate, so TLS verification works. Switching to PrivateLink is a DNS change only. |

---

## 5. Verification (orga, 9 Nov 2026)

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
| DNS from an `fl` pod on Fargate | `coordinator.fl.internal` → both coordinator IPs |
| CA key in CloudShell | `False` |

Live NetworkPolicy test (a pod labelled `app=cowrie` should print `DNS_OK` and `INTERNET_BLOCKED`): run for evidence if not yet done.

---

## 6. Differences from the agreed architecture

| Architecture says | What was built | Reason |
|---|---|---|
| Ideally separate AWS accounts | One account, separate VPCs | Simpler for a student project. PrivateLink and IAM still isolate. |
| Cowrie in a public subnet | Cowrie nodes in private honeypot subnets behind a public NLB | Still open to attackers; nodes have no public IPs. More secure. |
| No SG route from honeypot to FL | Cowrie **pods** fully blocked by NetworkPolicy; Cowrie **nodes** can still reach FL pods on 443 (nothing listens) | Closed fully by adding security groups for the FL Fargate pods (pending). |
| Security groups for Fargate FL pods | Not yet; FL pods use the cluster's default SG | Pending. |
| PrivateLink allowed principals = 3 org accounts | This account | Single-account setup. |
| NLB port 9092 | NLB 9092 → container 8000 | The repo's SuperLink listens on 8000. |
| Bucket `raw/`, `processed/` | `orga/raw/`, `orga/processed/` | Matches the repo's existing uploader layout. |

Additions not in the architecture: KMS keys, own CA and certificates, private DNS name, coordinator VPC endpoints, Pod Security labels, EKS secrets encryption and audit logs.

---

## 7. Known limitations (for the report)

- **EKS public API endpoint** is enabled so CloudShell can run `kubectl`. Access still requires AWS credentials. Hardening: restrict the allowed CIDRs, or go private-only with CloudShell in the VPC.
- **The load balancer controller runs on the Cowrie nodes.** A fully compromised node could misuse its permissions. Mitigated by IMDS hop limit 1, NetworkPolicies and Cowrie being an emulated shell. Better: a separate system node group.
- **The Cowrie node SG allows outbound 443 to anywhere** (needed for image pulls and AWS APIs). VPC endpoints for ECR/STS/EKS would allow tightening it.
- **NetworkPolicies do not apply to Fargate pods.** The FL side relies on Fargate microVM isolation, separate subnets and security groups.
- **FL pods use CoreDNS on the honeypot nodes.** A compromised node could spoof DNS, but TLS server-certificate verification would reject a fake coordinator.
- **Six teammate IAM users have AdministratorAccess** and are EKS cluster admins. This contradicts least privilege; MFA is required of each user. Alternative: namespace-scoped EKS access entries per team.
- **GuardDuty is unavailable** on this account (`SubscriptionRequiredException`). VPC flow logs are unconfirmed.
- **mTLS on the Flower side:** check whether flwr 1.40's SuperLink can require client certificates. If not, Flower SuperNode authentication provides the client-identity half.
- **Certificates expire after 90 days** (CA after 1 year). Rotation is manual.

---

## 8. Team handoff

| Team | Where | What they need |
|---|---|---|
| Coordinator | ECR `csc3104/coordinator` → ECS `csc3104-coordinator-cluster` | Push the image (new tag each time). Entrypoint writes `FL_TLS_CA_CERT`, `FL_TLS_SERVER_CERT`, `FL_TLS_SERVER_KEY` to files and starts `flower-superlink` with `--ssl-ca-certfile`, `--ssl-certfile`, `--ssl-keyfile` (not `--insecure`). Listen on 8000, checkpoints to `/models`, gRPC keepalive below 600 s. Then create the ECS service. |
| Cowrie | EKS `orga-honeypot-cluster`, namespace `default` | Bucket `csc3104-cowrie-orga-909891185696`, region `ap-southeast-1`, EFS ID and access point (`orga.json`). Change Service `loadBalancerClass` to `service.k8s.aws/nlb`. The PersistentVolume is cluster-wide, so the infra owner applies it. |
| Log processor | ECR `csc3104/log-processor` (to create) → EKS `default` | CronJob with `serviceAccountName: log-processor`; reads `orga/raw/`, writes `orga/processed/`; must not be labelled `app: cowrie`. |
| FL client | ECR `csc3104/fl-client` (to create) → EKS `fl` (Fargate) | `serviceAccountName: fl-client`; mount Secret `fl-client-tls`; connect to `coordinator.fl.internal:9092` with TLS; `FL_ORG_ID=orga`; data from `orga/processed/`; deploy as a Deployment so Kubernetes restarts it. |

Team access: IAM users Mervyn, WeiZhi, Amos, Andrew, JiaLer, ZhiYu (group `csc3104-admins`, sign-in `https://909891185696.signin.aws.amazon.com/console`), with EKS cluster-admin access entries on `orga-honeypot-cluster`.

---

## 9. What is left

1. Security groups for the orga FL Fargate pods (closes the honeypot → FL gap).
2. Build orgb and orgc (repeat steps 2.1–2.8 with `$Org = "orgb"` / `"orgc"`), then allowlist their NAT IPs and add EKS access entries.
3. Optional: PrivateLink interface endpoint per org.
4. Create the ECS coordinator service once the image is pushed; run the connection test.
5. Create ECR repos `csc3104/fl-client` and `csc3104/log-processor`; apply the Cowrie PersistentVolume.

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

Roughly **$11–12/day** for the coordinator plus orga; each extra org adds about $5–6/day. Tear down after the demo.

### Teardown order (when finished)
Delete in reverse dependency order:
1. ECS service, then EKS node groups / Fargate profiles / clusters.
2. NLBs and target groups, then the PrivateLink endpoint service.
3. NAT gateways, then release the Elastic IPs.
4. VPC endpoints, EFS (mount targets first), then subnets, route tables, IGWs and VPCs.
5. Empty and delete the S3 buckets, ECR repos, secrets and log groups.
6. Schedule the KMS keys for deletion.
7. Delete IAM roles, users and the OIDC provider.
