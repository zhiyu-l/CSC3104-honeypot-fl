param(
    [string]$Profile = 'cowrie-destination',
    [string]$Account = '909891185696',
    [string]$Region = 'ap-southeast-1',
    [string]$Tag = 'late-download-v1'
)

# Build/publish only; does not change EKS. Temporary file-based ECR credentials
# avoid the Windows credential-helper failure seen on the operator's laptop.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$registry = "$Account.dkr.ecr.$Region.amazonaws.com"
$image = "${registry}/cowrie-nightly:$Tag"
$identity = aws sts get-caller-identity --profile $Profile --output json
if ($LASTEXITCODE -ne 0) { throw 'AWS sign-in failed. Run aws login for the selected profile.' }
if (($identity | ConvertFrom-Json).Account -ne $Account) { throw 'Wrong AWS account' }

$dockerExe = (Get-Command docker).Source
$resourcesDir = Split-Path (Split-Path $dockerExe -Parent) -Parent
$pluginDir = Join-Path $resourcesDir 'cli-plugins'
if (!(Test-Path -LiteralPath (Join-Path $pluginDir 'docker-buildx.exe'))) {
    throw "Buildx plugin missing in $pluginDir"
}
$endpoint = docker context inspect desktop-linux --format '{{.Endpoints.docker.Host}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop Linux context unavailable' }
$endpoint = $endpoint.Trim()
$tempConfig = Join-Path ([IO.Path]::GetTempPath()) ('cowrie-ecr-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempConfig | Out-Null
$configFile = Join-Path $tempConfig 'config.json'

try {
    $token = aws ecr get-login-password --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'ECR authentication failed' }
    $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$($token.Trim())"))
    $entries = @{}
    $entries[$registry] = @{ auth = $auth }
    $config = @{ auths = $entries; cliPluginsExtraDirs = @($pluginDir) } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($configFile, $config, [Text.UTF8Encoding]::new($false))

    docker --config $tempConfig --host $endpoint buildx version
    if ($LASTEXITCODE -ne 0) { throw 'Buildx discovery failed' }
    docker --config $tempConfig --host $endpoint buildx build --platform linux/amd64,linux/arm64 `
        -f (Join-Path $repoRoot 'services/log-processor/nightly.Dockerfile') -t $image --push $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Image build/push failed' }
}
finally {
    if (Test-Path -LiteralPath $configFile) { Remove-Item -LiteralPath $configFile -Force }
    $token = $null
    $auth = $null
    $entries = $null
    $config = $null
}

$digest = aws ecr describe-images --profile $Profile --region $Region --repository-name cowrie-nightly `
    --image-ids "imageTag=$Tag" --query 'imageDetails[0].imageDigest' --output text
if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Image digest lookup failed' }
Write-Output "Published: ${registry}/cowrie-nightly@$digest"
