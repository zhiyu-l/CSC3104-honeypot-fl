# =====================================================================
# CSC3104 honeypot FL - coordinator infrastructure commands (AWS CLI, PowerShell)
# What was run in AWS CloudShell (pwsh) from 9 Nov 2026, with the fixes applied.
#
# CLIENT PATH: AWS PrivateLink. FL clients connect to coordinator.fl.internal:9092,
# which resolves to their own org's PrivateLink endpoint -> endpoint service
# csc3104-coord-privatelink -> internal NLB csc3104-coord-nlb-int -> task port 8000.
# The public NLB (step 1.12) is an unused fallback.
#
# DO NOT run this whole file in one go. Most steps create resources and are
# not safe to repeat (re-running creates duplicates). Paste one step at a time
# and check the output before continuing.
# =====================================================================


# ---------------------------------------------------------------------
# PART 0 - Session variables (run first in every new session)
# ---------------------------------------------------------------------
$Region    = "ap-southeast-1"
$env:AWS_DEFAULT_REGION = $Region
$AccountId = aws sts get-caller-identity --query Account --output text
$Prefix    = "csc3104"
$AZ1 = "ap-southeast-1a"; $AZ2 = "ap-southeast-1b"

# Working folder (forward slash: CloudShell is Linux)
$Work = "$HOME/csc3104-aws"; New-Item -ItemType Directory -Force $Work | Out-Null; Set-Location $Work
function Save-File($Name, $Text) { [IO.File]::WriteAllText((Join-Path $Work $Name), $Text) }  # UTF-8, no BOM

# Coordinator
$CoVpcCidr = "10.100.0.0/16"
$CoPubA = "10.100.0.0/24";  $CoPubB = "10.100.1.0/24"
$CoPrivA = "10.100.10.0/24"; $CoPrivB = "10.100.11.0/24"
$FleetPort     = 9092
$ContainerPort = 8000
$CoCluster    = "$Prefix-coordinator-cluster"
$CoService    = "$Prefix-coordinator"
$CoTaskFamily = "$Prefix-coordinator"
$CoRepo       = "$Prefix/coordinator"
$ModelsBucket = "$Prefix-fl-models-$AccountId"
$CoLogGroup   = "/ecs/$Prefix-coordinator"
$KmsAlias     = "alias/$Prefix-fl"
$TlsSecretName = "$Prefix/coordinator/tls"
$CoordinatorDns = "coordinator.fl.internal"
$EfsUid = 1000; $EfsGid = 1000

# Orgs (used by steps 1.11 and 1.15)
$Orgs = [ordered]@{
  orga = @{ Cidr = "10.0.0.0/16"; Octet = 0; Cluster = "orga-honeypot-cluster"; Bucket = "csc3104-cowrie-orga" }
  orgb = @{ Cidr = "10.1.0.0/16"; Octet = 1; Cluster = "orgb-honeypot-cluster"; Bucket = "csc3104-cowrie-orgb" }
  orgc = @{ Cidr = "10.2.0.0/16"; Octet = 2; Cluster = "orgc-honeypot-cluster"; Bucket = "csc3104-cowrie-orgc" }
}

# Save / restore progress (all created IDs)
function Save-Progress { Get-Variable Co*,Sg*,S3Pl,Kms*,Tls*,Efs*,Exec*,Task*,Nlb*,Tg*,Listener*,Eip*,Ep* -ErrorAction SilentlyContinue |
  ? { $_.Value -is [string] } | % -Begin { $h=@{} } -Process { $h[$_.Name]=$_.Value } -End { $h | ConvertTo-Json | Set-Content "$Work/progress.json" } }

# RESTORE after a reconnect (run pwsh, the block above, then this line):
# (Get-Content "$Work/progress.json" | ConvertFrom-Json).psobject.Properties | % { Set-Variable $_.Name $_.Value }

# Helper functions used in steps 1.1, 1.2 and 1.11 (paste again after a reconnect)
function New-Subnet($Vpc, $Cidr, $Az, $Name) {
  aws ec2 create-subnet --vpc-id $Vpc --cidr-block $Cidr --availability-zone $Az `
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$Name}]" --query Subnet.SubnetId --output text
}
function New-Sg($Name, $Desc) { aws ec2 create-security-group --vpc-id $CoVpc --group-name $Name --description $Desc --query GroupId --output text }
function Remove-DefaultEgress($Sg) { aws ec2 revoke-security-group-egress --group-id $Sg --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null }


# ---------------------------------------------------------------------
# 1.1 VPC, subnets and routing
# ---------------------------------------------------------------------
$CoVpc = aws ec2 create-vpc --cidr-block $CoVpcCidr --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$Prefix-coord-vpc}]" --query Vpc.VpcId --output text
aws ec2 modify-vpc-attribute --vpc-id $CoVpc --enable-dns-hostnames "Value=true"

$CoPubSubA  = New-Subnet $CoVpc $CoPubA  $AZ1 "$Prefix-coord-public-a"
$CoPubSubB  = New-Subnet $CoVpc $CoPubB  $AZ2 "$Prefix-coord-public-b"
$CoPrivSubA = New-Subnet $CoVpc $CoPrivA $AZ1 "$Prefix-coord-private-a"
$CoPrivSubB = New-Subnet $CoVpc $CoPrivB $AZ2 "$Prefix-coord-private-b"

$CoIgw = aws ec2 create-internet-gateway --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=$Prefix-coord-igw}]" --query InternetGateway.InternetGatewayId --output text
aws ec2 attach-internet-gateway --internet-gateway-id $CoIgw --vpc-id $CoVpc
$CoPubRt = aws ec2 create-route-table --vpc-id $CoVpc --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$Prefix-coord-public-rt}]" --query RouteTable.RouteTableId --output text
aws ec2 create-route --route-table-id $CoPubRt --destination-cidr-block 0.0.0.0/0 --gateway-id $CoIgw | Out-Null
aws ec2 associate-route-table --route-table-id $CoPubRt --subnet-id $CoPubSubA | Out-Null
aws ec2 associate-route-table --route-table-id $CoPubRt --subnet-id $CoPubSubB | Out-Null

# Private route table: deliberately NO 0.0.0.0/0 route
$CoPrivRt = aws ec2 create-route-table --vpc-id $CoVpc --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$Prefix-coord-private-rt}]" --query RouteTable.RouteTableId --output text
aws ec2 associate-route-table --route-table-id $CoPrivRt --subnet-id $CoPrivSubA | Out-Null
aws ec2 associate-route-table --route-table-id $CoPrivRt --subnet-id $CoPrivSubB | Out-Null

"VPC=$CoVpc PubA=$CoPubSubA PubB=$CoPubSubB PrivA=$CoPrivSubA PrivB=$CoPrivSubB IGW=$CoIgw PubRT=$CoPubRt PrivRT=$CoPrivRt"
Save-Progress


# ---------------------------------------------------------------------
# 1.2 Security groups
# ---------------------------------------------------------------------
$SgNlb  = New-Sg "$Prefix-coord-nlb-sg"  "Public fallback NLB - FL clients in"
$SgTask = New-Sg "$Prefix-coord-task-sg" "Coordinator Fargate task"
$SgEfs  = New-Sg "$Prefix-coord-efs-sg"  "Coordinator EFS"
$SgVpce = New-Sg "$Prefix-coord-vpce-sg" "Coordinator VPC endpoints"
foreach ($sg in $SgNlb, $SgTask, $SgEfs, $SgVpce) { Remove-DefaultEgress $sg }

$S3Pl = aws ec2 describe-managed-prefix-lists --filters "Name=prefix-list-name,Values=com.amazonaws.$Region.s3" --query "PrefixLists[0].PrefixListId" --output text

# NLB -> task only
aws ec2 authorize-security-group-egress --group-id $SgNlb --ip-permissions "IpProtocol=tcp,FromPort=$ContainerPort,ToPort=$ContainerPort,UserIdGroupPairs=[{GroupId=$SgTask}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $SgTask --protocol tcp --port $ContainerPort --source-group $SgNlb | Out-Null
# Task -> endpoints (443), S3 gateway (443), EFS (2049)
aws ec2 authorize-security-group-egress --group-id $SgTask --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=$SgVpce}]" | Out-Null
aws ec2 authorize-security-group-egress --group-id $SgTask --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=$S3Pl}]" | Out-Null
aws ec2 authorize-security-group-egress --group-id $SgTask --ip-permissions "IpProtocol=tcp,FromPort=2049,ToPort=2049,UserIdGroupPairs=[{GroupId=$SgEfs}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $SgEfs  --protocol tcp --port 2049 --source-group $SgTask | Out-Null
aws ec2 authorize-security-group-ingress --group-id $SgVpce --protocol tcp --port 443  --source-group $SgTask | Out-Null
# Public fallback NLB ingress from the org NAT IPs is added by org-setup.ps1
# (the internal NLB's SG for the PrivateLink path is created in step 1.11)

"NLB=$SgNlb TASK=$SgTask EFS=$SgEfs VPCE=$SgVpce S3PL=$S3Pl"
Save-Progress


# ---------------------------------------------------------------------
# 1.3 VPC endpoints (no internet egress for the coordinator)
# ---------------------------------------------------------------------
$CoVpceIds = @()
foreach ($svc in "ecr.api", "ecr.dkr", "logs", "secretsmanager", "ssmmessages") {
  $CoVpceIds += aws ec2 create-vpc-endpoint --vpc-id $CoVpc --vpc-endpoint-type Interface --service-name "com.amazonaws.$Region.$svc" `
    --subnet-ids $CoPrivSubA $CoPrivSubB --security-group-ids $SgVpce --private-dns-enabled `
    --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=$Prefix-coord-$svc}]" --query VpcEndpoint.VpcEndpointId --output text
}
$CoS3Vpce = aws ec2 create-vpc-endpoint --vpc-id $CoVpc --vpc-endpoint-type Gateway --service-name "com.amazonaws.$Region.s3" `
  --route-table-ids $CoPrivRt --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=$Prefix-coord-s3}]" --query VpcEndpoint.VpcEndpointId --output text

"Interface endpoints: $CoVpceIds"
"S3 gateway endpoint: $CoS3Vpce"
$CoVpceList = $CoVpceIds -join ","
Save-Progress


# ---------------------------------------------------------------------
# 1.4 KMS key
# ---------------------------------------------------------------------
Save-File "kms-policy.json" @"
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "AccountAdmin", "Effect": "Allow", "Principal": { "AWS": "arn:aws:iam::${AccountId}:root" }, "Action": "kms:*", "Resource": "*" },
    { "Sid": "CloudWatchLogs", "Effect": "Allow", "Principal": { "Service": "logs.${Region}.amazonaws.com" },
      "Action": ["kms:Encrypt*","kms:Decrypt*","kms:ReEncrypt*","kms:GenerateDataKey*","kms:Describe*"], "Resource": "*",
      "Condition": { "ArnLike": { "kms:EncryptionContext:aws:logs:arn": "arn:aws:logs:${Region}:${AccountId}:log-group:*" } } }
  ]
}
"@
$KmsKeyArn = aws kms create-key --description "CSC3104 FL encryption key" --policy file://kms-policy.json --query KeyMetadata.Arn --output text
aws kms create-alias --alias-name $KmsAlias --target-key-id $KmsKeyArn
aws kms enable-key-rotation --key-id $KmsKeyArn

"KMS key: $KmsKeyArn"
aws kms describe-key --key-id $KmsAlias --query "KeyMetadata.KeyState" --output text
Save-Progress


# ---------------------------------------------------------------------
# 1.5 CA, server certificate -> Secrets Manager
# ---------------------------------------------------------------------
# CA (valid 1 year)
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 365 -subj "/CN=CSC3104 FL Root CA" `
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt

# Server certificate for coordinator.fl.internal (valid 90 days)
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=$CoordinatorDns" -out server.csr
Save-File "server.ext" "basicConstraints=CA:FALSE`nkeyUsage=critical,digitalSignature`nextendedKeyUsage=serverAuth`nsubjectAltName=DNS:$CoordinatorDns`n"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 90 -sha256 -extfile server.ext -out server.crt

# Checks
openssl x509 -in server.crt -noout -subject -issuer -ext subjectAltName
openssl verify -CAfile ca.crt server.crt

# Store in Secrets Manager (KMS-encrypted), remove local key material
Save-File "tls-secret.json" (@{ ca_crt = (Get-Content ca.crt -Raw); server_crt = (Get-Content server.crt -Raw); server_key = (Get-Content server.key -Raw) } | ConvertTo-Json)
$TlsSecretArn = aws secretsmanager create-secret --name $TlsSecretName --kms-key-id $KmsAlias --secret-string file://tls-secret.json --query ARN --output text
Remove-Item tls-secret.json, server.key, server.csr
"TLS secret: $TlsSecretArn"
Save-Progress

# Then: CloudShell Actions -> Download file -> /home/cloudshell-user/csc3104-aws/ca.key
# Store it offline, then:
Remove-Item ca.key


# ---------------------------------------------------------------------
# 1.6 EFS for checkpoints
# ---------------------------------------------------------------------
$EfsId = aws efs create-file-system --encrypted --kms-key-id $KmsKeyArn --performance-mode generalPurpose --throughput-mode elastic `
  --tags "Key=Name,Value=$Prefix-coord-models" --query FileSystemId --output text
"EFS: $EfsId  (waiting until available...)"
do { Start-Sleep 5; $s = aws efs describe-file-systems --file-system-id $EfsId --query "FileSystems[0].LifeCycleState" --output text } while ($s -ne "available")

aws efs create-mount-target --file-system-id $EfsId --subnet-id $CoPrivSubA --security-groups $SgEfs --query MountTargetId --output text
aws efs create-mount-target --file-system-id $EfsId --subnet-id $CoPrivSubB --security-groups $SgEfs --query MountTargetId --output text

$EfsApId = aws efs create-access-point --file-system-id $EfsId --posix-user "Uid=$EfsUid,Gid=$EfsGid" `
  --root-directory "Path=/models,CreationInfo={OwnerUid=$EfsUid,OwnerGid=$EfsGid,Permissions=750}" `
  --tags "Key=Name,Value=$Prefix-coord-models-ap" --query AccessPointId --output text
$EfsArn   = "arn:aws:elasticfilesystem:${Region}:${AccountId}:file-system/$EfsId"
$EfsApArn = "arn:aws:elasticfilesystem:${Region}:${AccountId}:access-point/$EfsApId"

"EFS=$EfsId  AccessPoint=$EfsApId"
Save-Progress


# ---------------------------------------------------------------------
# 1.7 S3 bucket for the final model
# ---------------------------------------------------------------------
aws s3api create-bucket --bucket $ModelsBucket --create-bucket-configuration "LocationConstraint=$Region"
aws s3api put-public-access-block --bucket $ModelsBucket --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-versioning --bucket $ModelsBucket --versioning-configuration "Status=Enabled"

Save-File "models-enc.json" @"
{ "Rules": [ { "ApplyServerSideEncryptionByDefault": { "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "$KmsKeyArn" }, "BucketKeyEnabled": true } ] }
"@
aws s3api put-bucket-encryption --bucket $ModelsBucket --server-side-encryption-configuration file://models-enc.json

# Final bucket policy: TLS required only (the "writes only from coordinator VPC" rule was removed)
Save-File "models-bucket-policy.json" @"
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "DenyNonTLS", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
      "Resource": ["arn:aws:s3:::$ModelsBucket", "arn:aws:s3:::$ModelsBucket/*"],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } } }
  ]
}
"@
aws s3api put-bucket-policy --bucket $ModelsBucket --policy file://models-bucket-policy.json

# Checks
aws s3api get-bucket-encryption --bucket $ModelsBucket --query "ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm" --output text
aws s3api get-bucket-versioning --bucket $ModelsBucket --query Status --output text
aws s3api get-bucket-policy --bucket $ModelsBucket --query Policy --output text | ConvertFrom-Json | % { $_.Statement.Sid }

# Upload test
"test" | Set-Content test.txt
aws s3 cp test.txt "s3://$ModelsBucket/test.txt"
aws s3 rm "s3://$ModelsBucket/test.txt"; Remove-Item test.txt
"Bucket: $ModelsBucket"
Save-Progress


# ---------------------------------------------------------------------
# 1.8 ECR repository and log group
# ---------------------------------------------------------------------
$CoRepoUri = aws ecr create-repository --repository-name $CoRepo --image-tag-mutability IMMUTABLE `
  --image-scanning-configuration "scanOnPush=true" --encryption-configuration "encryptionType=KMS,kmsKey=$KmsKeyArn" `
  --query repository.repositoryUri --output text

aws logs create-log-group --log-group-name $CoLogGroup --kms-key-id $KmsKeyArn
aws logs put-retention-policy --log-group-name $CoLogGroup --retention-in-days 30

"ECR repo: $CoRepoUri"
aws logs describe-log-groups --log-group-name-prefix $CoLogGroup --query "logGroups[0].[logGroupName,retentionInDays,kmsKeyId]" --output text
Save-Progress


# ---------------------------------------------------------------------
# 1.9 IAM roles
# ---------------------------------------------------------------------
Save-File "ecs-trust.json" @"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "Service": "ecs-tasks.amazonaws.com" },
  "Action": "sts:AssumeRole", "Condition": { "StringEquals": { "aws:SourceAccount": "$AccountId" } } } ] }
"@

# Execution role (ECS starts the task: pull image, write logs, read TLS secret)
$ExecRoleArn = aws iam create-role --role-name "$Prefix-coordinator-exec-role" --assume-role-policy-document file://ecs-trust.json --query Role.Arn --output text
aws iam attach-role-policy --role-name "$Prefix-coordinator-exec-role" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
Save-File "exec-inline.json" @"
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "$TlsSecretArn" },
  { "Effect": "Allow", "Action": "kms:Decrypt", "Resource": "$KmsKeyArn" } ] }
"@
aws iam put-role-policy --role-name "$Prefix-coordinator-exec-role" --policy-name tls-secret-read --policy-document file://exec-inline.json

# Task role (what the coordinator itself can do)
$TaskRoleArn = aws iam create-role --role-name "$Prefix-coordinator-task-role" --assume-role-policy-document file://ecs-trust.json --query Role.Arn --output text
Save-File "task-inline.json" @"
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "EfsViaAccessPoint", "Effect": "Allow", "Action": ["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite"],
    "Resource": "$EfsArn", "Condition": { "StringEquals": { "elasticfilesystem:AccessPointArn": "$EfsApArn" } } },
  { "Sid": "WriteFinalModel", "Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::$ModelsBucket/*" },
  { "Sid": "KmsForS3", "Effect": "Allow", "Action": ["kms:GenerateDataKey","kms:Decrypt"], "Resource": "$KmsKeyArn" },
  { "Sid": "EcsExec", "Effect": "Allow", "Action": ["ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel","ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel"], "Resource": "*" } ] }
"@
aws iam put-role-policy --role-name "$Prefix-coordinator-task-role" --policy-name coordinator-task --policy-document file://task-inline.json

"Exec role: $ExecRoleArn"
"Task role: $TaskRoleArn"
aws iam list-role-policies --role-name "$Prefix-coordinator-task-role" --output text
Save-Progress


# ---------------------------------------------------------------------
# 1.10 EFS file system policy
# ---------------------------------------------------------------------
Save-File "efs-policy.json" @"
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "DenyNonTLS", "Effect": "Deny", "Principal": { "AWS": "*" }, "Action": "*", "Resource": "$EfsArn",
    "Condition": { "Bool": { "aws:SecureTransport": "false" } } },
  { "Sid": "CoordinatorOnly", "Effect": "Allow", "Principal": { "AWS": "$TaskRoleArn" },
    "Action": ["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite"], "Resource": "$EfsArn",
    "Condition": { "StringEquals": { "elasticfilesystem:AccessPointArn": "$EfsApArn" } } } ] }
"@
aws efs put-file-system-policy --file-system-id $EfsId --policy file://efs-policy.json --query "Policy" --output text | ConvertFrom-Json | % { $_.Statement.Sid }
Save-Progress


# ---------------------------------------------------------------------
# 1.11 PrivateLink path (PRIMARY): internal NLB + endpoint service
# ---------------------------------------------------------------------
# This is how the FL clients reach the coordinator. Each org creates an interface
# endpoint to this service inside its own VPC (org-setup.ps1 step 2.9 / step 1.15
# below for orga), so FL traffic never crosses the public internet and the
# coordinator is never exposed publicly. Acceptance is required: only endpoint
# connections we accept can reach it.
$SgNlbInt = New-Sg "$Prefix-coord-nlb-int-sg" "Internal NLB - PrivateLink"
Remove-DefaultEgress $SgNlbInt
foreach ($o in $Orgs.Values) { aws ec2 authorize-security-group-ingress --group-id $SgNlbInt --protocol tcp --port $FleetPort --cidr $o.Cidr | Out-Null }
aws ec2 authorize-security-group-egress --group-id $SgNlbInt --ip-permissions "IpProtocol=tcp,FromPort=$ContainerPort,ToPort=$ContainerPort,UserIdGroupPairs=[{GroupId=$SgTask}]" | Out-Null
aws ec2 authorize-security-group-ingress --group-id $SgTask --protocol tcp --port $ContainerPort --source-group $SgNlbInt | Out-Null

$NlbIntArn = aws elbv2 create-load-balancer --name "$Prefix-coord-nlb-int" --type network --scheme internal `
  --subnets $CoPrivSubA $CoPrivSubB --security-groups $SgNlbInt --query "LoadBalancers[0].LoadBalancerArn" --output text
"Internal NLB: $NlbIntArn  (waiting...)"
aws elbv2 wait load-balancer-available --load-balancer-arns $NlbIntArn
aws elbv2 modify-load-balancer-attributes --load-balancer-arn $NlbIntArn --attributes "Key=load_balancing.cross_zone.enabled,Value=true" | Out-Null
$TgIntArn = aws elbv2 create-target-group --name "$Prefix-coord-tg-int" --protocol TCP --port $ContainerPort --vpc-id $CoVpc `
  --target-type ip --health-check-protocol TCP --query "TargetGroups[0].TargetGroupArn" --output text
aws elbv2 modify-target-group-attributes --target-group-arn $TgIntArn --attributes "Key=deregistration_delay.timeout_seconds,Value=30" | Out-Null
$ListenerIntArn = aws elbv2 create-listener --load-balancer-arn $NlbIntArn --protocol TCP --port $FleetPort `
  --default-actions "Type=forward,TargetGroupArn=$TgIntArn" --query "Listeners[0].ListenerArn" --output text
aws elbv2 modify-listener-attributes --listener-arn $ListenerIntArn --attributes "Key=tcp.idle_timeout.seconds,Value=600" | Out-Null

$EpSvcId = aws ec2 create-vpc-endpoint-service-configuration --network-load-balancer-arns $NlbIntArn --acceptance-required `
  --tag-specifications "ResourceType=vpc-endpoint-service,Tags=[{Key=Name,Value=$Prefix-coord-privatelink}]" `
  --query ServiceConfiguration.ServiceId --output text
aws ec2 modify-vpc-endpoint-service-permissions --service-id $EpSvcId --add-allowed-principals "arn:aws:iam::${AccountId}:root" | Out-Null
$EpSvcName = aws ec2 describe-vpc-endpoint-service-configurations --service-ids $EpSvcId --query "ServiceConfigurations[0].ServiceName" --output text

"Endpoint service: $EpSvcId"
"PrivateLink service name: $EpSvcName"
Save-Progress



# ---------------------------------------------------------------------
# 1.12 Public NLB with static IPs (FALLBACK ONLY - not used by the deployment)
# ---------------------------------------------------------------------
# Kept as a static-IP fallback for a client that cannot use PrivateLink.
# The ECS service is NOT registered with this NLB's target group, so it has no
# targets. Skip this step for a PrivateLink-only build, or delete it later
# (saves about USD 0.70/day and removes an exposed address).
$Eip1 = aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$Prefix-coord-nlb-a}]" --query AllocationId --output text
$Eip2 = aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$Prefix-coord-nlb-b}]" --query AllocationId --output text

$NlbArn = aws elbv2 create-load-balancer --name "$Prefix-coord-nlb" --type network --scheme internet-facing `
  --subnet-mappings "SubnetId=$CoPubSubA,AllocationId=$Eip1" "SubnetId=$CoPubSubB,AllocationId=$Eip2" `
  --security-groups $SgNlb --query "LoadBalancers[0].LoadBalancerArn" --output text
"NLB: $NlbArn  (waiting until active, ~2-3 min...)"
aws elbv2 wait load-balancer-available --load-balancer-arns $NlbArn
aws elbv2 modify-load-balancer-attributes --load-balancer-arn $NlbArn --attributes "Key=load_balancing.cross_zone.enabled,Value=true" | Out-Null

$TgArn = aws elbv2 create-target-group --name "$Prefix-coord-tg" --protocol TCP --port $ContainerPort --vpc-id $CoVpc `
  --target-type ip --health-check-protocol TCP --query "TargetGroups[0].TargetGroupArn" --output text
aws elbv2 modify-target-group-attributes --target-group-arn $TgArn --attributes "Key=deregistration_delay.timeout_seconds,Value=30" | Out-Null

$ListenerArn = aws elbv2 create-listener --load-balancer-arn $NlbArn --protocol TCP --port $FleetPort `
  --default-actions "Type=forward,TargetGroupArn=$TgArn" --query "Listeners[0].ListenerArn" --output text
aws elbv2 modify-listener-attributes --listener-arn $ListenerArn --attributes "Key=tcp.idle_timeout.seconds,Value=600" | Out-Null

$CoPublicIps = (aws ec2 describe-addresses --allocation-ids $Eip1 $Eip2 --query "Addresses[].PublicIp" --output text) -replace "\s+", " "
$NlbDns = aws elbv2 describe-load-balancers --load-balancer-arns $NlbArn --query "LoadBalancers[0].DNSName" --output text
"Coordinator static IPs: $CoPublicIps"
"NLB DNS: $NlbDns"
Save-Progress


# ---------------------------------------------------------------------
# 1.13 Part A - ECS cluster and task definition (DONE)
# ---------------------------------------------------------------------
$CoImageTag = "v1"   # change to the tag the app team pushes

aws ecs create-cluster --cluster-name $CoCluster --settings "name=containerInsights,value=enabled" --query "cluster.status" --output text

Save-File "taskdef.json" @"
{
  "family": "$CoTaskFamily",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024", "memory": "2048",
  "runtimePlatform": { "operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64" },
  "executionRoleArn": "$ExecRoleArn",
  "taskRoleArn": "$TaskRoleArn",
  "containerDefinitions": [ {
    "name": "superlink",
    "image": "${CoRepoUri}:$CoImageTag",
    "essential": true,
    "portMappings": [ { "containerPort": $ContainerPort, "protocol": "tcp" } ],
    "environment": [ { "name": "FL_MODEL_DIR", "value": "/models" } ],
    "secrets": [
      { "name": "FL_TLS_CA_CERT",     "valueFrom": "${TlsSecretArn}:ca_crt::" },
      { "name": "FL_TLS_SERVER_CERT", "valueFrom": "${TlsSecretArn}:server_crt::" },
      { "name": "FL_TLS_SERVER_KEY",  "valueFrom": "${TlsSecretArn}:server_key::" } ],
    "mountPoints": [ { "sourceVolume": "models", "containerPath": "/models", "readOnly": false } ],
    "linuxParameters": { "initProcessEnabled": true },
    "logConfiguration": { "logDriver": "awslogs", "options": {
      "awslogs-group": "$CoLogGroup", "awslogs-region": "$Region", "awslogs-stream-prefix": "superlink" } }
  } ],
  "volumes": [ { "name": "models", "efsVolumeConfiguration": {
    "fileSystemId": "$EfsId", "transitEncryption": "ENABLED",
    "authorizationConfig": { "accessPointId": "$EfsApId", "iam": "ENABLED" } } } ]
}
"@
$TaskDefArn = aws ecs register-task-definition --cli-input-json file://taskdef.json --query "taskDefinition.taskDefinitionArn" --output text
"Task definition: $TaskDefArn"
Save-Progress


# ---------------------------------------------------------------------
# 1.13 Part B - ECS service, registered with the INTERNAL NLB only (PrivateLink)
# ---------------------------------------------------------------------
# Created by the coordinator team once the image was in ECR (service
# csc3104-coordinator-service, task definition revision 6, which adds
# FL_SUPERNODE_PUBLIC_KEYS and runs /app/aws-start.sh). Equivalent command:
#
# aws ecr describe-images --repository-name $CoRepo --query "imageDetails[].imageTags" --output text
#
# $CoService = "$Prefix-coordinator-service"
# aws ecs create-service --cluster $CoCluster --service-name $CoService --task-definition $CoTaskFamily `
#   --desired-count 1 --launch-type FARGATE --platform-version LATEST `
#   --network-configuration "awsvpcConfiguration={subnets=[$CoPrivSubA,$CoPrivSubB],securityGroups=[$SgTask],assignPublicIp=DISABLED}" `
#   --load-balancers "targetGroupArn=$TgIntArn,containerName=superlink,containerPort=$ContainerPort" `
#   --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100,deploymentCircuitBreaker={enable=true,rollback=true}" `
#   --health-check-grace-period-seconds 60 --enable-execute-command --query "service.status" --output text
#
# (Only the internal target group: the task is reachable through PrivateLink only.
#  To also use the public fallback, add "targetGroupArn=$TgArn,..." as a second --load-balancers entry.)
#
# aws ecs wait services-stable --cluster $CoCluster --services $CoService
# aws ecs describe-services --cluster $CoCluster --services $CoService --query "services[0].{running:runningCount,desired:desiredCount,lbs:loadBalancers[].targetGroupArn}"
#
# If it fails to stabilise:
# aws ecs describe-services --cluster $CoCluster --services $CoService --query "services[0].events[0:5].message"


# ---------------------------------------------------------------------
# 1.14 Connection test through PrivateLink (after the service runs)
# ---------------------------------------------------------------------
# Target behind the internal NLB (expect: healthy)
aws elbv2 describe-target-health --target-group-arn $TgIntArn --query "TargetHealthDescriptions[].TargetHealth.State" --output text

# Endpoint connections from the orgs (expect 3, all "available")
aws ec2 describe-vpc-endpoint-connections --filters "Name=service-id,Values=$EpSvcId" `
  --query "VpcEndpointConnections[].[VpcEndpointId,VpcEndpointState]" --output table

# From inside an org: TCP + TLS to the coordinator through that org's endpoint
# (run with kubectl pointing at the org's cluster; the pod lands on Fargate in the FL subnet)
# kubectl run pl-test -n fl --image=alpine:3.20 --restart=Never --rm -i --pod-running-timeout=4m -- `
#   sh -c "apk add -q openssl >/dev/null; nslookup coordinator.fl.internal; echo | openssl s_client -connect coordinator.fl.internal:9092 -servername coordinator.fl.internal -brief 2>&1 | head -5"
# Expect: the org's endpoint IP (e.g. 10.0.20.100) and "CONNECTION ESTABLISHED".
# (Certificate verification needs ca.crt in the pod; the handshake alone proves the PrivateLink path.)
#
# Shell into the running task (ECS Exec):
# $TaskArn = aws ecs list-tasks --cluster $CoCluster --service-name $CoService --query "taskArns[0]" --output text
# aws ecs execute-command --cluster $CoCluster --task $TaskArn --container superlink --interactive --command "/bin/sh"


# ---------------------------------------------------------------------
# 1.15 PrivateLink endpoint for an org that is already built (used for orga)
# ---------------------------------------------------------------------
# orgb and orgc get this automatically in org-setup.ps1 step 2.9. For orga
# (built by hand) run this block with the org's IDs. It creates the interface
# endpoint in the org's FL subnets, accepts it on the coordinator side, and
# points coordinator.fl.internal (the org's private zone) at the endpoint.
function Connect-OrgPrivateLink($Org, $VpcId, $FlSubA, $FlSubB, $ZoneId) {
  $n = $Orgs[$Org].Octet
  $sg = aws ec2 create-security-group --vpc-id $VpcId --group-name "$Prefix-$Org-coord-endpoint-sg" `
          --description "PrivateLink endpoint to the coordinator" --query GroupId --output text
  aws ec2 revoke-security-group-egress --group-id $sg --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" | Out-Null
  foreach ($c in "10.$n.20.0/24", "10.$n.21.0/24") {   # FL subnets only, not the honeypot subnets
    aws ec2 authorize-security-group-ingress --group-id $sg --ip-permissions "IpProtocol=tcp,FromPort=$FleetPort,ToPort=$FleetPort,IpRanges=[{CidrIp=$c,Description=fl-subnet}]" | Out-Null
  }
  $ep = aws ec2 create-vpc-endpoint --vpc-id $VpcId --vpc-endpoint-type Interface --service-name $EpSvcName `
          --subnet-ids $FlSubA $FlSubB --security-group-ids $sg --no-private-dns-enabled `
          --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=$Prefix-$Org-coord-endpoint}]" `
          --query VpcEndpoint.VpcEndpointId --output text
  Start-Sleep 15
  aws ec2 accept-vpc-endpoint-connections --service-id $EpSvcId --vpc-endpoint-ids $ep | Out-Null
  do { Start-Sleep 15; $st = aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $ep --query "VpcEndpoints[0].State" --output text; "  endpoint $ep : $st" } while ($st -ne "available")
  $enis = (aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $ep --query "VpcEndpoints[0].NetworkInterfaceIds" --output text) -split "\s+"
  $ips  = (aws ec2 describe-network-interfaces --network-interface-ids $enis --query "NetworkInterfaces[].PrivateIpAddress" --output text) -split "\s+"
  $rr   = ($ips | % { "{ `"Value`": `"$_`" }" }) -join ", "
  Save-File "$Org-dns-privatelink.json" @"
{ "Changes": [ { "Action": "UPSERT", "ResourceRecordSet": {
  "Name": "$CoordinatorDns", "Type": "A", "TTL": 60, "ResourceRecords": [ $rr ] } } ] }
"@
  aws route53 change-resource-record-sets --hosted-zone-id $ZoneId --change-batch "file://$Org-dns-privatelink.json" --query ChangeInfo.Status --output text
  "$Org : endpoint $ep, SG $sg, coordinator.fl.internal -> $($ips -join ', ')"
  "Use the IP in the FL-a subnet (10.$n.20.x) in deploy/k8s/overlays/aws/fl/sg-$Org/tls.patch.yaml (hostAliases)."
}
# Example for orga (IDs from orga.json - check the key names match, orga was built by hand):
# $oa = Get-Content "$Work/orga.json" | ConvertFrom-Json
# Connect-OrgPrivateLink "orga" $oa.Vpc $oa.FlA $oa.FlB $oa.ZoneId


# ---------------------------------------------------------------------
# Baseline (SKIPPED)
# ---------------------------------------------------------------------
# GuardDuty: failed with SubscriptionRequiredException (not available on this account)
# aws guardduty create-detector --enable --query DetectorId --output text
#
# VPC flow logs: ran, printed no ID, unconfirmed. To check:
# aws ec2 describe-flow-logs --filter "Name=resource-id,Values=$CoVpc" --query "FlowLogs[].[FlowLogId,FlowLogStatus,DeliverLogsStatus,LogDestination]" --output text
# aws ec2 create-flow-logs --resource-type VPC --resource-ids $CoVpc --traffic-type ALL `
#   --log-destination-type s3 --log-destination "arn:aws:s3:::$ModelsBucket/flowlogs/coordinator/" --query "FlowLogIds" --output text
