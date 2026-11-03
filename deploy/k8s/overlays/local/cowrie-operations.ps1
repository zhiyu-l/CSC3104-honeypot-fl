[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('credentials', 'upload', 'pause', 'resume', 'status')]
    [string]$Action,
    [Parameter(Mandatory=$true)]
    [ValidateSet('orga', 'orgb', 'orgc')]
    [string]$Org,
    [string]$Profile
)
$ErrorActionPreference = 'Stop'
$context = "kind-$Org-honeypot-cluster"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../../../..')).Path
$overlay = Join-Path $PSScriptRoot $Org

function Invoke-Kube {
    & kubectl --context $context --namespace default @args
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed for $context." }
}

switch ($Action) {
    'credentials' {
        if (-not $Profile) { $Profile = "cowrie-$Org" }
        # Capture only in memory. Never print exported credentials or save them in Git.
        $raw = & aws configure export-credentials --profile $Profile --format process
        if ($LASTEXITCODE -ne 0) { throw "Could not resolve AWS profile $Profile." }
        $credentials = $raw | ConvertFrom-Json
        if (-not $credentials.AccessKeyId -or -not $credentials.SecretAccessKey) {
            throw 'The profile returned incomplete credentials.'
        }
        $values = @{
            AWS_ACCESS_KEY_ID = $credentials.AccessKeyId
            AWS_SECRET_ACCESS_KEY = $credentials.SecretAccessKey
        }
        if ($credentials.SessionToken) { $values.AWS_SESSION_TOKEN = $credentials.SessionToken }
        $encoded = @{}
        foreach ($name in $values.Keys) {
            $encoded[$name] = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($values[$name]))
        }
        $secret = @{
            apiVersion = 'v1'; kind = 'Secret'; type = 'Opaque'
            metadata = @{ name = 'cowrie-aws-credentials'; namespace = 'default' }
            data = $encoded
        } | ConvertTo-Json -Depth 6
        # Direct pipeline: a plain wrapper function would not forward stdin.
        $secret | & kubectl --context $context --namespace default apply --server-side --field-manager=cowrie-local-credentials -f -
        if ($LASTEXITCODE -ne 0) { throw 'Could not apply credential Secret.' }
        if ($credentials.Expiration) {
            Write-Warning "Credential snapshot expires at $($credentials.Expiration). Refresh before expiry; this script does not auto-renew it."
        }
        $raw = $credentials = $values = $encoded = $secret = $null
    }
    'upload' {
        $job = 'cowrie-upload-' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + (Get-Random -Minimum 1000 -Maximum 9999)
        Invoke-Kube create job $job --from=cronjob/cowrie-logs-to-s3
        & kubectl --context $context --namespace default wait --for=condition=complete "job/$job" --timeout=300s
        $waitExit = $LASTEXITCODE
        Invoke-Kube logs "job/$job"
        if ($waitExit -ne 0) { throw "Job $job did not complete within five minutes; inspect it before retrying." }
    }
    { $_ -in 'pause', 'resume' } {
        $suspended = if ($Action -eq 'pause') { 'true' } else { 'false' }
        $source = Join-Path $overlay 'upload.patch.yaml'
        $content = Get-Content -Raw $source
        $pattern = '(?m)^(\s*suspend:\s*)(true|false)(\s*)$'
        if ([regex]::Matches($content, $pattern).Count -ne 1) { throw 'Expected one suspend setting in upload.patch.yaml.' }
        $updated = [regex]::Replace($content, $pattern, ('${1}' + $suspended + '${3}'))
        $localDir = Join-Path $repo '.local/cowrie'
        New-Item -ItemType Directory -Force $localDir | Out-Null
        $patch = Join-Path $localDir "$Org-suspend.json"
        ('{"spec":{"suspend":' + $suspended + '}}') | Set-Content -Encoding ascii $patch
        # Patch only suspend: JSON merge patches replace container arrays.
        Invoke-Kube patch cronjob cowrie-logs-to-s3 --type=merge --patch-file $patch
        $updated | Set-Content -Encoding ascii $source
        Invoke-Kube get cronjob cowrie-logs-to-s3
    }
    'status' {
        Invoke-Kube get 'pods,pvc,hpa,cronjobs,jobs'
    }
}
