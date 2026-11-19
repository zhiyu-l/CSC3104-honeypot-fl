<#
  CSC3104 honeypot FL - per-organisation AWS infrastructure (steps 2.1 - 2.9)
  Same setup as orga, with all fixes from the orga build included.

  CLIENT PATH: AWS PrivateLink. Step 2.9 creates this org's interface endpoint to
  the coordinator's endpoint service (in the FL subnets), accepts it, and points
  coordinator.fl.internal at it. FL traffic never crosses the public internet.
  The NAT allowlist on the coordinator's public NLB (step 2.1) is only for the
  unused static-IP fallback.

  HOW TO RUN (AWS CloudShell, Singapore region):
    1. Actions -> Upload file -> this file (org-setup.ps1)
    2. Actions -> Upload file -> ca.key (from your PC)
    3. In CloudShell:   pwsh -File ~/org-setup.ps1 -Org orgb
       (later for orgc: pwsh -File ~/org-setup.ps1 -Org orgc, upload ca.key again first)

  The script stops at the first error. It prints which section failed.
  Do NOT simply re-run it after a failure (it would create duplicates) - check the
  log ($HOME/csc3104-aws/<org>-setup.log) and resume from the failed section by hand.
  Takes about 30-40 minutes (EKS cluster creation is the slow part).
#>
param(
  [ValidateSet("orgb", "orgc")]
  [string]$Org = "orgb"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

# =====================================================================
# Part 0 - shared settings (same as the coordinator and orga)
# =====================================================================
$Region    = "ap-southeast-1"
$env:AWS_DEFAULT_REGION = $Region
$AccountId = aws sts get-caller-identity --query Account --output text
$Prefix    = "csc3104"
$AZ1 = "ap-southeast-1a"; $AZ2 = "ap-southeast-1b"
$FleetPort = 9092
$CoordinatorDns = "coordinator.fl.internal"
$Team = "Mervyn", "WeiZhi", "Amos", "Andrew", "JiaLer", "ZhiYu"

$Work = "$HOME/csc3104-aws"; New-Item -ItemType Directory -Force $Work | Out-Null; Set-Location $Work
function Save-File($Name, $Text) { [IO.File]::WriteAllText((Join-Path $Work $Name), $Text) }

$Orgs = [ordered]@{
  orga = @{ Cidr = "10.0.0.0/16"; Octet = 0; Cluster = "orga-honeypot-cluster"; Bucket = "csc3104-cowrie-orga" }
  orgb = @{ Cidr = "10.1.0.0/16"; Octet = 1; Cluster = "orgb-honeypot-cluster"; Bucket = "csc3104-cowrie-orgb" }
  orgc = @{ Cidr = "10.2.0.0/16"; Octet = 2; Cluster = "orgc-honeypot-cluster"; Bucket = "csc3104-cowrie-orgc" }
}
$O = $Orgs[$Org]; $n = $O.Octet
$S = @{}
function Save-Org { $S | ConvertTo-Json | Set-Content "$Work/$Org.json" }
function Section($Text) { Write-Host "`n==================== $Text ====================" -ForegroundColor Cyan }

Start-Transcript -Path "$Work/$Org-setup.log" -Append | Out-Null

# --- Pre-flight checks (fail early, before anything is created) ---
Section "Pre-flight checks for $Org"
if (Test-Path "$Work/$Org.json") { throw "$Work/$Org.json already exists - $Org may already be (partly) built. Stop and check." }
if (-not (Test-Path "$HOME/ca.key")) { throw "ca.key not found in $HOME. Upload it first (Actions -> Upload file)." }
if (-not (Test-Path "$Work/ca.crt")) { throw "ca.crt not found in $Work (created in coordinator step 1.5)." }
if (-not (Test-Path "$Work/progress.json")) { throw "Coordinator progress.json not found in $Work." }
(Get-Content "$Work/progress.json" | ConvertFrom-Json).psobject.Properties | % { Set-Variable $_.Name $_.Value }
if (-not $EpSvcId -or -not $EpSvcName) { throw "Coordinator PrivateLink variables (EpSvcId, EpSvcName) missing from progress.json (coordinator step 1.11)." }
"Account $AccountId | org $Org | VPC $($O.Cidr) | PrivateLink service $EpSvcName"

try {
# =====================================================================
# 2.1 Network: VPC, subnets, NAT (static IP), S3 endpoint, coordinator allowlist
# =====================================================================
Section "2.1 Network"
function New-OrgSubnet($Cidr, $Az, $Name, $RoleTag) {
  aws ec2 create-subnet --vpc-id $S.Vpc --cidr-block $Cidr --availability-zone $Az `
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$Name},{Key=$RoleTag,Value=1}]" --query Subnet.SubnetId --output text
}
$S.Vpc = aws ec2 create-vpc --cidr-block $O.Cidr --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$Prefix-$Org-vpc}]" --query Vpc.VpcId --output text
aws ec2 modify-vpc-attribute --vpc-id $S.Vpc --enable-dns-hostnames "Value=true"

$S.PubA = New-OrgSubnet "10.$n.0.0/24"  $AZ1 "$Prefix-$Org-public-a"   "kubernetes.io/role/elb"
$S.PubB = New-OrgSubnet "10.$n.1.0/24"  $AZ2 "$Prefix-$Org-public-b"   "kubernetes.io/role/elb"
$S.HpA  = New-OrgSubnet "10.$n.10.0/24" $AZ1 "$Prefix-$Org-honeypot-a" "kubernetes.io/role/internal-elb"
$S.HpB  = New-OrgSubnet "10.$n.11.0/24" $AZ2 "$Prefix-$Org-honeypot-b" "kubernetes.io/role/internal-elb"
$S.FlA  = New-OrgSubnet "10.$n.20.0/24" $AZ1 "$Prefix-$Org-fl-a"       "kubernetes.io/role/internal-elb"
$S.FlB  = New-OrgSubnet "10.$n.21.0/24" $AZ2 "$Prefix-$Org-fl-b"       "kubernetes.io/role/internal-elb"
Save-Org

$S.Igw = aws ec2 create-internet-gateway --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=$Prefix-$Org-igw}]" --query InternetGateway.InternetGatewayId --output text
aws ec2 attach-internet-gateway --internet-gateway-id $S.Igw --vpc-id $S.Vpc
$S.PubRt = aws ec2 create-route-table --vpc-id $S.Vpc --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$Prefix-$Org-public-rt}]" --query RouteTable.RouteTableId --output text
aws ec2 create-route --route-table-id $S.PubRt --destination-cidr-block 0.0.0.0/0 --gateway-id $S.Igw | Out-Null
foreach ($sub in $S.PubA, $S.PubB) { aws ec2 associate-route-table --route-table-id $S.PubRt --subnet-id $sub | Out-Null }

$S.NatEip = aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$Prefix-$Org-nat}]" --query AllocationId --output text
$S.Nat = aws ec2 create-nat-gateway --subnet-id $S.PubA --allocation-id $S.NatEip `
  --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=$Prefix-$Org-nat}]" --query NatGateway.NatGatewayId --output text
"NAT $($S.Nat) creating (~2 min)..."
aws ec2 wait nat-gateway-available --nat-gateway-ids $S.Nat

$S.PrivRt = aws ec2 create-route-table --vpc-id $S.Vpc --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$Prefix-$Org-private-rt}]" --query RouteTable.RouteTableId --output text
aws ec2 create-route --route-table-id $S.PrivRt --destination-cidr-block 0.0.0.0/0 --nat-gateway-id $S.Nat | Out-Null
foreach ($sub in $S.HpA, $S.HpB, $S.FlA, $S.FlB) { aws ec2 associate-route-table --route-table-id $S.PrivRt --subnet-id $sub | Out-Null }

$S.S3Vpce = aws ec2 create-vpc-endpoint --vpc-id $S.Vpc --vpc-endpoint-type Gateway --service-name "com.amazonaws.$Region.s3" `
  --route-table-ids $S.PrivRt --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=$Prefix-$Org-s3}]" --query VpcEndpoint.VpcEndpointId --output text

$S.NatIp = aws ec2 describe-addresses --allocation-ids $S.NatEip --query "Addresses[0].PublicIp" --output text
# NAT = outbound for image pulls and AWS APIs. FL traffic does NOT use it (PrivateLink, step 2.9).
# Allowlist on the public fallback NLB only, if it exists:
if ($SgNlb) { aws ec2 authorize-security-group-ingress --group-id $SgNlb --ip-permissions "IpProtocol=tcp,FromPort=$FleetPort,ToPort=$FleetPort,IpRanges=[{CidrIp=$($S.NatIp)/32,Description=$Org-nat}]" | Out-Null }
Save-Org
"VPC $($S.Vpc) | NAT static IP $($S.NatIp)"

# =====================================================================
# 2.2 KMS key, S3 bucket, Cowrie EFS
# =====================================================================
Section "2.2 KMS, S3, EFS"
Save-File "$Org-kms-policy.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Sid": "AccountAdmin", "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::${AccountId}:root" }, "Action": "kms:*", "Resource": "*" } ] }
"@
$S.KmsArn = aws kms create-key --description "CSC3104 $Org encryption key" --policy "file://$Org-kms-policy.json" --query KeyMetadata.Arn --output text
aws kms create-alias --alias-name "alias/$Prefix-$Org" --target-key-id $S.KmsArn
aws kms enable-key-rotation --key-id $S.KmsArn
Save-Org

$S.Bucket = $O.Bucket
try { aws s3api create-bucket --bucket $S.Bucket --create-bucket-configuration "LocationConstraint=$Region" 2>$null | Out-Null }
catch {
  $S.Bucket = "$($O.Bucket)-$AccountId"
  aws s3api create-bucket --bucket $S.Bucket --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
}
aws s3api put-public-access-block --bucket $S.Bucket --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-versioning --bucket $S.Bucket --versioning-configuration "Status=Enabled"
Save-File "$Org-s3-enc.json" @"
{ "Rules": [ { "ApplyServerSideEncryptionByDefault": { "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "$($S.KmsArn)" }, "BucketKeyEnabled": true } ] }
"@
aws s3api put-bucket-encryption --bucket $S.Bucket --server-side-encryption-configuration "file://$Org-s3-enc.json"
Save-File "$Org-s3-policy.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Sid": "DenyNonTLS", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
  "Resource": ["arn:aws:s3:::$($S.Bucket)", "arn:aws:s3:::$($S.Bucket)/*"],
  "Condition": { "Bool": { "aws:SecureTransport": "false" } } } ] }
"@
aws s3api put-bucket-policy --bucket $S.Bucket --policy "file://$Org-s3-policy.json"
Save-Org

$S.SgEfs = aws ec2 create-security-group --vpc-id $S.Vpc --group-name "$Prefix-$Org-efs-sg" --description "Cowrie EFS" --query GroupId --output text
aws ec2 revoke-security-group-egress --group-id $S.SgEfs --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null
$S.EfsId = aws efs create-file-system --encrypted --kms-key-id $S.KmsArn --performance-mode generalPurpose --throughput-mode elastic `
  --tags "Key=Name,Value=$Prefix-$Org-cowrie-logs" --query FileSystemId --output text
do { Start-Sleep 5; $st = aws efs describe-file-systems --file-system-id $S.EfsId --query "FileSystems[0].LifeCycleState" --output text } while ($st -ne "available")
aws efs create-mount-target --file-system-id $S.EfsId --subnet-id $S.HpA --security-groups $S.SgEfs --query MountTargetId --output text
aws efs create-mount-target --file-system-id $S.EfsId --subnet-id $S.HpB --security-groups $S.SgEfs --query MountTargetId --output text
$S.EfsApId = aws efs create-access-point --file-system-id $S.EfsId --posix-user "Uid=999,Gid=999" `
  --root-directory "Path=/$Org,CreationInfo={OwnerUid=999,OwnerGid=999,Permissions=750}" `
  --tags "Key=Name,Value=$Prefix-$Org-cowrie-ap" --query AccessPointId --output text
$S.EfsArn = "arn:aws:elasticfilesystem:${Region}:${AccountId}:file-system/$($S.EfsId)"
Save-File "$Org-efs-policy.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Sid": "DenyNonTLS", "Effect": "Deny", "Principal": { "AWS": "*" }, "Action": "*",
  "Resource": "$($S.EfsArn)", "Condition": { "Bool": { "aws:SecureTransport": "false" } } } ] }
"@
aws efs put-file-system-policy --file-system-id $S.EfsId --policy "file://$Org-efs-policy.json" | Out-Null
Save-Org
"Bucket $($S.Bucket) | EFS $($S.EfsId) / $($S.EfsApId)"

# =====================================================================
# 2.3 EKS cluster (secrets encrypted with org KMS key, audit logs)
# =====================================================================
Section "2.3 EKS cluster (10-15 min)"
Save-File "eks-cluster-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "eks.amazonaws.com" },
  "Action": ["sts:AssumeRole", "sts:TagSession"] } ] }
"@
$S.ClusterRoleArn = aws iam create-role --role-name "$Prefix-$Org-eks-cluster-role" --assume-role-policy-document file://eks-cluster-trust.json --query Role.Arn --output text
aws iam attach-role-policy --role-name "$Prefix-$Org-eks-cluster-role" --policy-arn arn:aws:iam::aws:policy/AmazonEKSClusterPolicy
Start-Sleep 15

Save-File "$Org-eks-encryption.json" "[ { `"resources`": [`"secrets`"], `"provider`": { `"keyArn`": `"$($S.KmsArn)`" } } ]"
Save-File "$Org-eks-logging.json" '{ "clusterLogging": [ { "types": ["api", "audit", "authenticator"], "enabled": true } ] }'
aws eks create-cluster --name $O.Cluster --role-arn $S.ClusterRoleArn `
  --resources-vpc-config "subnetIds=$($S.HpA),$($S.HpB),$($S.FlA),$($S.FlB),endpointPublicAccess=true,endpointPrivateAccess=true" `
  --encryption-config "file://$Org-eks-encryption.json" `
  --logging "file://$Org-eks-logging.json" `
  --access-config "authenticationMode=API_AND_CONFIG_MAP" `
  --query "cluster.status" --output text
Save-Org
aws eks wait cluster-active --name $O.Cluster

$S.ClusterSg  = aws eks describe-cluster --name $O.Cluster --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text
$S.OidcIssuer = aws eks describe-cluster --name $O.Cluster --query "cluster.identity.oidc.issuer" --output text
$S.ClusterArn = aws eks describe-cluster --name $O.Cluster --query "cluster.arn" --output text
Save-Org
aws eks update-kubeconfig --name $O.Cluster --alias $O.Cluster   # kubectl now points at this org
kubectl get svc

# =====================================================================
# 2.4 Cowrie node group (IMDSv2 hop 1, own SG) + Fargate profile for fl
# =====================================================================
Section "2.4 Node group + Fargate profile (5-8 min)"
$S.HpNodeSg = aws ec2 create-security-group --vpc-id $S.Vpc --group-name "$Prefix-$Org-cowrie-node-sg" --description "Cowrie honeypot nodes" --query GroupId --output text
aws ec2 revoke-security-group-egress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=-1,UserIdGroupPairs=[{GroupId=$($S.ClusterSg)}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=-1,UserIdGroupPairs=[{GroupId=$($S.HpNodeSg)}]" | Out-Null
aws ec2 authorize-security-group-egress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null
aws ec2 authorize-security-group-egress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=-1,UserIdGroupPairs=[{GroupId=$($S.HpNodeSg)}]" | Out-Null
aws ec2 authorize-security-group-egress --group-id $S.HpNodeSg --ip-permissions "IpProtocol=tcp,FromPort=2049,ToPort=2049,UserIdGroupPairs=[{GroupId=$($S.SgEfs)}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $S.ClusterSg --protocol tcp --port 443 --source-group $S.HpNodeSg | Out-Null
aws ec2 authorize-security-group-ingress --group-id $S.SgEfs --protocol tcp --port 2049 --source-group $S.HpNodeSg | Out-Null
Save-Org

Save-File "ec2-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "ec2.amazonaws.com" }, "Action": "sts:AssumeRole" } ] }
"@
$S.NodeRoleArn = aws iam create-role --role-name "$Prefix-$Org-cowrie-node-role" --assume-role-policy-document file://ec2-trust.json --query Role.Arn --output text
foreach ($p in "AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy", "AmazonSSMManagedInstanceCore") {
  aws iam attach-role-policy --role-name "$Prefix-$Org-cowrie-node-role" --policy-arn "arn:aws:iam::aws:policy/$p"
}

Save-File "$Org-lt.json" @"
{
  "SecurityGroupIds": ["$($S.HpNodeSg)"],
  "MetadataOptions": { "HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled" },
  "BlockDeviceMappings": [ { "DeviceName": "/dev/xvda", "Ebs": { "VolumeSize": 20, "VolumeType": "gp3", "Encrypted": true } } ],
  "TagSpecifications": [ { "ResourceType": "instance", "Tags": [ { "Key": "Name", "Value": "$Prefix-$Org-cowrie-node" } ] } ]
}
"@
$S.LtId = aws ec2 create-launch-template --launch-template-name "$Prefix-$Org-cowrie-lt" --launch-template-data "file://$Org-lt.json" --query LaunchTemplate.LaunchTemplateId --output text
Save-Org
Start-Sleep 15
aws eks create-nodegroup --cluster-name $O.Cluster --nodegroup-name "$Org-cowrie-ng" `
  --node-role $S.NodeRoleArn --subnets $S.HpA $S.HpB `
  --launch-template "id=$($S.LtId),version=1" --ami-type AL2023_x86_64_STANDARD --instance-types t3.small `
  --scaling-config "minSize=1,maxSize=3,desiredSize=2" --labels "role=honeypot" `
  --query "nodegroup.status" --output text

Save-File "$Org-fargate-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "eks-fargate-pods.amazonaws.com" },
  "Action": "sts:AssumeRole",
  "Condition": { "ArnLike": { "aws:SourceArn": "arn:aws:eks:${Region}:${AccountId}:fargateprofile/$($O.Cluster)/*" } } } ] }
"@
$S.FargateRoleArn = aws iam create-role --role-name "$Prefix-$Org-fargate-pod-role" --assume-role-policy-document "file://$Org-fargate-trust.json" --query Role.Arn --output text
aws iam attach-role-policy --role-name "$Prefix-$Org-fargate-pod-role" --policy-arn arn:aws:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy
Save-Org
Start-Sleep 15
aws eks create-fargate-profile --cluster-name $O.Cluster --fargate-profile-name "$Org-fl-profile" `
  --pod-execution-role-arn $S.FargateRoleArn --subnets $S.FlA $S.FlB --selectors "namespace=fl" `
  --query "fargateProfile.status" --output text

aws eks wait nodegroup-active --cluster-name $O.Cluster --nodegroup-name "$Org-cowrie-ng"
aws eks wait fargate-profile-active --cluster-name $O.Cluster --fargate-profile-name "$Org-fl-profile"
kubectl get nodes -o wide

# =====================================================================
# 2.5 Add-ons, namespaces, Pod Security, NetworkPolicies
# =====================================================================
Section "2.5 Add-ons + NetworkPolicies"
Save-File "vpc-cni-config.json" '{"enableNetworkPolicy":"true"}'
aws eks create-addon --cluster-name $O.Cluster --addon-name vpc-cni --configuration-values "file://vpc-cni-config.json" `
  --resolve-conflicts OVERWRITE --query addon.status --output text
foreach ($a in "kube-proxy", "coredns", "eks-pod-identity-agent", "aws-efs-csi-driver", "metrics-server") {
  "$a : " + (aws eks create-addon --cluster-name $O.Cluster --addon-name $a --resolve-conflicts OVERWRITE --query addon.status --output text)
}
foreach ($a in "vpc-cni", "kube-proxy", "coredns", "eks-pod-identity-agent", "aws-efs-csi-driver", "metrics-server") {
  aws eks wait addon-active --cluster-name $O.Cluster --addon-name $a; "$a ACTIVE"
}

kubectl create namespace fl
kubectl label namespace default pod-security.kubernetes.io/enforce=baseline pod-security.kubernetes.io/warn=restricted --overwrite
kubectl label namespace fl      pod-security.kubernetes.io/enforce=baseline pod-security.kubernetes.io/warn=restricted --overwrite

@'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny-all, namespace: default }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-dns, namespace: default }
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
  - to:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
      podSelector: { matchLabels: { k8s-app: kube-dns } }
    ports: [ { protocol: UDP, port: 53 }, { protocol: TCP, port: 53 } ]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: cowrie-allow-ssh-in, namespace: default }
spec:
  podSelector: { matchLabels: { app: cowrie } }
  policyTypes: [Ingress]
  ingress:
  - ports: [ { protocol: TCP, port: 2222 } ]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: jobs-allow-aws-egress, namespace: default }
spec:
  podSelector:
    matchExpressions: [ { key: app, operator: NotIn, values: [cowrie] } ]
  policyTypes: [Egress]
  egress:
  - to: [ { ipBlock: { cidr: 169.254.170.23/32 } } ]
    ports: [ { protocol: TCP, port: 80 } ]
  - to: [ { ipBlock: { cidr: 0.0.0.0/0, except: [10.0.0.0/8, 169.254.0.0/16] } } ]
    ports: [ { protocol: TCP, port: 443 } ]
'@ | kubectl apply -f -

# =====================================================================
# 2.6 IAM for pods (Pod Identity + IRSA), LB controller, team access
# =====================================================================
Section "2.6 IAM, LB controller, team access"
$IssuerHost = $S.OidcIssuer -replace '^https://', ''
$B = $S.Bucket

function New-PodIdentityRole($RoleName, $Ns, $Sa, $PolicyName, $PolicyJson) {
  Save-File "$RoleName-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "pods.eks.amazonaws.com" },
  "Action": ["sts:AssumeRole", "sts:TagSession"],
  "Condition": { "StringEquals": {
    "aws:RequestTag/kubernetes-namespace": "$Ns",
    "aws:RequestTag/kubernetes-service-account": "$Sa",
    "aws:RequestTag/eks-cluster-arn": "$($S.ClusterArn)" } } } ] }
"@
  $arn = aws iam create-role --role-name $RoleName --assume-role-policy-document "file://$RoleName-trust.json" --query Role.Arn --output text
  if ($PolicyJson) {
    Save-File "$RoleName-policy.json" $PolicyJson
    aws iam put-role-policy --role-name $RoleName --policy-name $PolicyName --policy-document "file://$RoleName-policy.json"
  }
  aws eks create-pod-identity-association --cluster-name $O.Cluster --namespace $Ns --service-account $Sa --role-arn $arn --query "association.associationId" --output text | Out-Null
  return $arn
}

$S.UploaderRoleArn = New-PodIdentityRole "$Prefix-$Org-cowrie-uploader" "default" "cowrie-log-uploader" "upload-raw" @"
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "UploadOrganisationLogs", "Effect": "Allow", "Action": ["s3:PutObject", "s3:AbortMultipartUpload"], "Resource": "arn:aws:s3:::$B/$Org/raw/*" },
  { "Sid": "KmsForUpload", "Effect": "Allow", "Action": "kms:GenerateDataKey", "Resource": "$($S.KmsArn)" } ] }
"@
$S.ProcessorRoleArn = New-PodIdentityRole "$Prefix-$Org-log-processor" "default" "log-processor" "raw-to-processed" @"
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "ListRaw", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::$B", "Condition": { "StringLike": { "s3:prefix": ["$Org/raw/*"] } } },
  { "Sid": "ReadRaw", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$B/$Org/raw/*" },
  { "Sid": "WriteProcessed", "Effect": "Allow", "Action": ["s3:PutObject", "s3:AbortMultipartUpload"], "Resource": "arn:aws:s3:::$B/$Org/processed/*" },
  { "Sid": "Kms", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey"], "Resource": "$($S.KmsArn)" } ] }
"@
Save-Org

$S.OidcProviderArn = aws iam create-open-id-connect-provider --url $S.OidcIssuer --client-id-list sts.amazonaws.com --query OpenIDConnectProviderArn --output text
Save-File "$Org-fl-client-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Federated": "$($S.OidcProviderArn)" },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": { "StringEquals": { "${IssuerHost}:sub": "system:serviceaccount:fl:fl-client", "${IssuerHost}:aud": "sts.amazonaws.com" } } } ] }
"@
$S.FlClientRoleArn = aws iam create-role --role-name "$Prefix-$Org-fl-client" --assume-role-policy-document "file://$Org-fl-client-trust.json" --query Role.Arn --output text
Save-File "$Org-fl-client-policy.json" @"
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "ListProcessed", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::$B", "Condition": { "StringLike": { "s3:prefix": ["$Org/processed/*"] } } },
  { "Sid": "ReadProcessed", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$B/$Org/processed/*" },
  { "Sid": "Kms", "Effect": "Allow", "Action": "kms:Decrypt", "Resource": "$($S.KmsArn)" } ] }
"@
aws iam put-role-policy --role-name "$Prefix-$Org-fl-client" --policy-name read-processed --policy-document "file://$Org-fl-client-policy.json"
Save-Org

kubectl create serviceaccount cowrie-log-uploader -n default
kubectl create serviceaccount log-processor -n default
kubectl create serviceaccount fl-client -n fl
kubectl annotate serviceaccount fl-client -n fl "eks.amazonaws.com/role-arn=$($S.FlClientRoleArn)" --overwrite

# AWS Load Balancer Controller (IAM policy is shared and already exists from orga)
$LbcPolicyArn = "arn:aws:iam::${AccountId}:policy/$Prefix-AWSLoadBalancerControllerIAMPolicy"
try { aws iam get-policy --policy-arn $LbcPolicyArn --query Policy.Arn --output text | Out-Null }
catch {
  Invoke-WebRequest -Uri "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/main/docs/install/iam_policy.json" -OutFile lbc-iam-policy.json
  $LbcPolicyArn = aws iam create-policy --policy-name "$Prefix-AWSLoadBalancerControllerIAMPolicy" --policy-document file://lbc-iam-policy.json --query Policy.Arn --output text
}
$S.LbcRoleArn = New-PodIdentityRole "$Prefix-$Org-lb-controller" "kube-system" "aws-load-balancer-controller" $null $null
aws iam attach-role-policy --role-name "$Prefix-$Org-lb-controller" --policy-arn $LbcPolicyArn
Save-Org

if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
  curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3; bash get_helm.sh
}
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system `
  --set clusterName=$($O.Cluster) --set region=$Region --set vpcId=$($S.Vpc) `
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller --set replicaCount=1

# Team access: EKS cluster admin for each teammate
foreach ($u in $Team) {
  $arn = "arn:aws:iam::${AccountId}:user/$u"
  aws eks create-access-entry --cluster-name $O.Cluster --principal-arn $arn | Out-Null
  aws eks associate-access-policy --cluster-name $O.Cluster --principal-arn $arn `
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy --access-scope "type=cluster" | Out-Null
  "$u : EKS cluster admin on $($O.Cluster)"
}

# =====================================================================
# 2.7 Client certificate, private DNS zone for the coordinator name
# =====================================================================
Section "2.7 Client certificate + DNS"
Move-Item "$HOME/ca.key" "$Work/ca.key" -Force
try {
  openssl ecparam -name prime256v1 -genkey -noout -out "$Org-client.key"
  openssl req -new -key "$Org-client.key" -subj "/CN=$Org" -out "$Org-client.csr"
  Save-File "$Org-client.ext" "basicConstraints=CA:FALSE`nkeyUsage=critical,digitalSignature`nextendedKeyUsage=clientAuth`nsubjectAltName=DNS:$Org.fl.internal`n"
  openssl x509 -req -in "$Org-client.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 90 -sha256 -extfile "$Org-client.ext" -out "$Org-client.crt"
}
finally { Remove-Item "$Work/ca.key" -Force -ErrorAction SilentlyContinue }   # CA key always removed again
"CA key still in CloudShell: $(Test-Path "$Work/ca.key")"
openssl verify -CAfile ca.crt "$Org-client.crt"
openssl x509 -in "$Org-client.crt" -noout -subject -ext extendedKeyUsage

Save-File "$Org-client-secret.json" (@{ ca_crt = (Get-Content ca.crt -Raw); client_crt = (Get-Content "$Org-client.crt" -Raw); client_key = (Get-Content "$Org-client.key" -Raw) } | ConvertTo-Json)
$S.ClientSecretArn = aws secretsmanager create-secret --name "$Prefix/$Org/fl-client-tls" --kms-key-id $S.KmsArn --secret-string "file://$Org-client-secret.json" --query ARN --output text
kubectl create secret generic fl-client-tls -n fl --from-file=ca.crt=ca.crt --from-file=tls.crt="$Org-client.crt" --from-file=tls.key="$Org-client.key"
Remove-Item "$Org-client-secret.json", "$Org-client.key", "$Org-client.csr"
Save-Org

$S.ZoneId = (aws route53 create-hosted-zone --name fl.internal --caller-reference "$Org-$(Get-Date -Format yyyyMMddHHmmss)" `
  --vpc "VPCRegion=$Region,VPCId=$($S.Vpc)" --hosted-zone-config "Comment=$Org FL private zone,PrivateZone=true" `
  --query HostedZone.Id --output text) -replace '/hostedzone/', ''
Save-Org
"Zone $($S.ZoneId) created; coordinator.fl.internal is pointed at the PrivateLink endpoint in step 2.9"

# =====================================================================
# 2.9 PrivateLink endpoint to the coordinator (the FL client's only path)
# =====================================================================
Section "2.9 PrivateLink endpoint"
# Endpoint SG: port 9092 in from the FL subnets only (the honeypot subnets cannot reach the coordinator)
$S.SgCoordEp = aws ec2 create-security-group --vpc-id $S.Vpc --group-name "$Prefix-$Org-coord-endpoint-sg" `
  --description "PrivateLink endpoint to the coordinator" --query GroupId --output text
aws ec2 revoke-security-group-egress --group-id $S.SgCoordEp --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null
foreach ($c in "10.$n.20.0/24", "10.$n.21.0/24") {
  aws ec2 authorize-security-group-ingress --group-id $S.SgCoordEp --ip-permissions "IpProtocol=tcp,FromPort=$FleetPort,ToPort=$FleetPort,IpRanges=[{CidrIp=$c,Description=fl-subnet}]" | Out-Null
}

# Interface endpoint in the FL subnets (private DNS off: the name is handled by our own zone)
$S.CoordEp = aws ec2 create-vpc-endpoint --vpc-id $S.Vpc --vpc-endpoint-type Interface --service-name $EpSvcName `
  --subnet-ids $S.FlA $S.FlB --security-group-ids $S.SgCoordEp --no-private-dns-enabled `
  --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=$Prefix-$Org-coord-endpoint}]" `
  --query VpcEndpoint.VpcEndpointId --output text
Save-Org

# Accept it on the coordinator side (endpoint service requires acceptance)
Start-Sleep 15
aws ec2 accept-vpc-endpoint-connections --service-id $EpSvcId --vpc-endpoint-ids $S.CoordEp | Out-Null
do {
  Start-Sleep 15
  $st = aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $S.CoordEp --query "VpcEndpoints[0].State" --output text
  "  endpoint $($S.CoordEp): $st"
} while ($st -ne "available")

# Endpoint IPs (one per FL subnet) -> coordinator.fl.internal
$enis = (aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $S.CoordEp --query "VpcEndpoints[0].NetworkInterfaceIds" --output text) -split "\s+"
$S.CoordEpIps = ((aws ec2 describe-network-interfaces --network-interface-ids $enis --query "NetworkInterfaces[].PrivateIpAddress" --output text) -split "\s+") -join " "
$rr = ($S.CoordEpIps.Split(' ') | % { "{ `"Value`": `"$_`" }" }) -join ", "
Save-File "$Org-dns-privatelink.json" @"
{ "Changes": [ { "Action": "UPSERT", "ResourceRecordSet": {
  "Name": "$CoordinatorDns", "Type": "A", "TTL": 60, "ResourceRecords": [ $rr ] } } ] }
"@
aws route53 change-resource-record-sets --hosted-zone-id $S.ZoneId --change-batch "file://$Org-dns-privatelink.json" --query ChangeInfo.Status --output text
Save-Org
"PrivateLink endpoint $($S.CoordEp): coordinator.fl.internal -> $($S.CoordEpIps)"
"For the repo: put the 10.$n.20.x address in deploy/k8s/overlays/aws/fl/sg-$Org/tls.patch.yaml (hostAliases)."

}
catch {
  Save-Org
  Write-Host "`nFAILED: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "Saved IDs so far in $Work/$Org.json. Do NOT re-run the whole script - send the error and the last section name." -ForegroundColor Red
  Stop-Transcript | Out-Null
  exit 1
}

# =====================================================================
# 2.8 Checks
# =====================================================================
Section "2.8 $Org security checklist"
$ErrorActionPreference = "Continue"; $PSNativeCommandUseErrorActionPreference = $false
function Check($Name, $Value) { "{0,-45} {1}" -f $Name, ($Value -join " | ") }
"Waiting 60 s for the LB controller and nodes to settle..."; Start-Sleep 60
Check "VPC" $S.Vpc
Check "NAT static IP" $S.NatIp
Check "PrivateLink endpoint state" (aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $S.CoordEp --query "VpcEndpoints[0].State" --output text)
Check "PrivateLink endpoint IPs" $S.CoordEpIps
Check "Endpoint SG ingress (9092)" ((aws ec2 describe-security-groups --group-ids $S.SgCoordEp --query "SecurityGroups[0].IpPermissions[].IpRanges[].CidrIp" --output text) -replace "\s+", ", ")
Check "S3 bucket" $S.Bucket
Check "S3 encryption" (aws s3api get-bucket-encryption --bucket $S.Bucket --query "ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm" --output text)
Check "S3 versioning" (aws s3api get-bucket-versioning --bucket $S.Bucket --query Status --output text)
Check "S3 public access fully blocked" (aws s3api get-public-access-block --bucket $S.Bucket --query "PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]" --output text)
Check "Cowrie EFS encrypted / mount targets" @((aws efs describe-file-systems --file-system-id $S.EfsId --query "FileSystems[0].Encrypted" --output text), (aws efs describe-mount-targets --file-system-id $S.EfsId --query "length(MountTargets)" --output text))
Check "EKS status / version" (aws eks describe-cluster --name $O.Cluster --query "[cluster.status,cluster.version]" --output text)
Check "EKS secrets encrypted with KMS" (aws eks describe-cluster --name $O.Cluster --query "cluster.encryptionConfig[0].resources" --output text)
Check "EKS audit logging" (aws eks describe-cluster --name $O.Cluster --query "cluster.logging.clusterLogging[?enabled].types[]" --output text)
Check "Cowrie nodes Ready" ((kubectl get nodes --no-headers | Select-String " Ready").Count)
Check "Cowrie node IMDS (tokens, hop limit)" (aws ec2 describe-instances --filters "Name=tag:Name,Values=$Prefix-$Org-cowrie-node" "Name=instance-state-name,Values=running" --query "Reservations[].Instances[].[MetadataOptions.HttpTokens,MetadataOptions.HttpPutResponseHopLimit]" --output text)
Check "Fargate profile (fl)" (aws eks describe-fargate-profile --cluster-name $O.Cluster --fargate-profile-name "$Org-fl-profile" --query "fargateProfile.status" --output text)
Check "Add-ons" ((aws eks list-addons --cluster-name $O.Cluster --query "addons" --output text) -replace "\s+", ", ")
Check "Network policy enforcement" ((kubectl get ds aws-node -n kube-system -o jsonpath="{.spec.template.spec.containers[?(@.name=='aws-eks-nodeagent')].args}") -match "enable-network-policy=true")
Check "NetworkPolicies in default" ((kubectl get networkpolicy -n default --no-headers).Count)
Check "Pod Identity associations" ((aws eks list-pod-identity-associations --cluster-name $O.Cluster --query "associations[].serviceAccount" --output text) -replace "\s+", ", ")
Check "fl-client IRSA role" (kubectl get sa fl-client -n fl -o jsonpath="{.metadata.annotations.eks\.amazonaws\.com/role-arn}")
Check "fl-client-tls secret keys" ((kubectl get secret fl-client-tls -n fl -o jsonpath="{.data}" | ConvertFrom-Json).psobject.Properties.Name -join ", ")
Check "LB controller ready" (kubectl get deploy aws-load-balancer-controller -n kube-system -o jsonpath="{.status.readyReplicas}/{.spec.replicas}")
Check "EKS access entries (teammates)" ((aws eks list-access-entries --cluster-name $O.Cluster --query "accessEntries" --output text) -split "\s+" | ? { $_ -like "*:user/*" } | % { $_.Split('/')[-1] })
Check "coordinator.fl.internal (expect endpoint IPs)" ((aws route53 list-resource-record-sets --hosted-zone-id $S.ZoneId --query "ResourceRecordSets[?Name=='coordinator.fl.internal.'].ResourceRecords[].Value" --output text) -replace "\s+", ", ")
Check "CA key in CloudShell" (Test-Path "$Work/ca.key")

Section "Live tests"
"DNS + TCP through PrivateLink from the fl namespace (Fargate pod, 1-2 min; TCP_OK needs the coordinator service running):"
kubectl run pl-test -n fl --image=busybox:1.37 --restart=Never --rm -i --pod-running-timeout=4m -- `
  sh -c "nslookup coordinator.fl.internal; nc -w 5 coordinator.fl.internal $FleetPort </dev/null && echo TCP_OK || echo TCP_FAIL"
"NetworkPolicy test (expect DNS_OK and INTERNET_BLOCKED):"
kubectl run np-test --image=busybox:1.37 --labels app=cowrie --restart=Never --rm -i -n default -- `
  sh -c "sleep 5; nslookup kubernetes.default.svc.cluster.local >/dev/null && echo DNS_OK; wget -T 5 -qO- http://example.com >/dev/null 2>&1 && echo INTERNET_OPEN || echo INTERNET_BLOCKED"

Save-Org
Section "DONE: $Org"
"IDs saved in $Work/$Org.json, full log in $Work/$Org-setup.log"
"kubectl now points at $($O.Cluster). Switch back with: kubectl config use-context orga-honeypot-cluster"
"Keep ca.key on your PC only (it was deleted from CloudShell)."
Stop-Transcript | Out-Null
