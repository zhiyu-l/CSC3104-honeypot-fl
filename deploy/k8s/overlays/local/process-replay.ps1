[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('render', 'run')]
    [string]$Action,
    [Parameter(Mandatory=$true)]
    [ValidateSet('orga', 'orgb', 'orgc')]
    [string]$Org,
    [Parameter(Mandatory=$true)]
    [string]$Manifest,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9./:@_-]*$')]
    [string]$Image = 'cowrie-log-processor:local-v1'
)
$ErrorActionPreference = 'Stop'
$context = "kind-$Org-honeypot-cluster"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../../../..')).Path
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
if ((Get-Item -LiteralPath $manifestPath).Length -gt 524288) { throw 'Manifest exceeds 512 KiB.' }
$selection = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($selection.manifest_version -ne 1 -or $selection.organisation -cne $Org) {
    throw 'Expected manifest_version 1 and matching organisation.'
}
$runId = [string]$selection.run_id
if ($runId -cnotmatch '^[a-z0-9]([a-z0-9-]{0,34}[a-z0-9])?$') { throw 'Use a lowercase run ID of 1-36 letters/digits/hyphens.' }
if (@($selection.sessions).Count -lt 1 -or @($selection.sessions).Count -gt 10000) { throw 'Select 1-10000 sessions.' }
$seen = @{}
foreach ($session in $selection.sessions) {
    if ($session.pod_uid -cnotmatch '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' -or
        $session.session_id -cnotmatch '^[a-zA-Z0-9_-]{1,128}$' -or $session.session_id -match 'REPLACE_') {
        throw 'Replace each pod_uid and session_id with real replay identifiers.'
    }
    $key = "$($session.pod_uid)/$($session.session_id)"
    if ($seen.ContainsKey($key)) { throw 'Duplicate selected session.' }
    $seen[$key] = $true
}
# Resolve destinations from the organisation overlay, never from implicit AWS defaults.
$settings = Get-Content -Raw (Join-Path $PSScriptRoot "$Org/kustomization.yaml")
$bucketMatch = [regex]::Matches($settings, '(?m)^\s*- S3_BUCKET=([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])\s*$')
$regionMatch = [regex]::Matches($settings, '(?m)^\s*- AWS_DEFAULT_REGION=([a-z0-9-]+)\s*$')
if ($bucketMatch.Count -ne 1 -or $regionMatch.Count -ne 1) { throw 'Expected one literal bucket and region in the organisation overlay.' }
$name = "cowrie-features-$Org-$runId"
$output = Join-Path $repo ".local/cowrie/processed-runs/$Org/$runId"
New-Item -ItemType Directory -Force $output | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$config = @{
    apiVersion = 'v1'; kind = 'ConfigMap'; immutable = $true
    metadata = @{ name = $name; namespace = 'default'; labels = @{ organisation = $Org; 'replay-run' = $runId } }
    data = @{
        'replay.json' = ($selection | ConvertTo-Json -Depth 12)
        'upload_features.sh' = ((Get-Content -Raw (Join-Path $repo 'services/log-processor/upload_features.sh')).Replace("`r`n", "`n"))
    }
}
$configPath = Join-Path $output 'selection.configmap.json'
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 16), $utf8)
$template = Get-Content -Raw (Join-Path $PSScriptRoot 'log-processor/job.template.yaml')
$job = $template.Replace('__ORG__', $Org).Replace('__RUN_ID__', $runId).Replace('__IMAGE__', $Image).Replace('__BUCKET__', $bucketMatch[0].Groups[1].Value).Replace('__REGION__', $regionMatch[0].Groups[1].Value)
$jobPath = Join-Path $output 'job.yaml'
[IO.File]::WriteAllText($jobPath, $job, $utf8)
Write-Host "Rendered $name for $context in $output"
if ($Action -eq 'render') { return }

function Invoke-Kube {
    & kubectl --context $context --namespace default @args
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed for $context. See the run guide for diagnostics." }
}
# Create (not apply) so an existing run selection/Job cannot be silently replaced.
Invoke-Kube create -f $configPath
Invoke-Kube create -f $jobPath
& kubectl --context $context --namespace default wait --for=condition=complete "job/$name" --timeout=300s
$waitExit = $LASTEXITCODE
& kubectl --context $context --namespace default logs "job/$name" -c process
if ($waitExit -ne 0) {
    throw "Run not complete after five minutes. Inspect $name before retrying; the Job may still be running."
}
Invoke-Kube logs "job/$name" -c upload
