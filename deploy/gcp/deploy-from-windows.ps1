# Despliegue AInvestor a GCP e2-micro desde Windows
param(
    [Parameter(Mandatory = $true)][string]$VmIp,
    [string]$SshUser = $env:USERNAME,
    [string]$SshKey = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$LocalPath = "C:\Users\alexf\Desktop\AInvestor",
    [string]$RemotePath = "/opt/ainvestor"
)

$ErrorActionPreference = "Stop"

$sshArgs = @()
if (Test-Path $SshKey) { $sshArgs = @("-i", $SshKey) }

Write-Host "==> Comprobando SSH a ${SshUser}@${VmIp}..."
ssh @sshArgs -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "${SshUser}@${VmIp}" "echo ok"

Write-Host "==> Preparando directorio remoto..."
ssh @sshArgs "${SshUser}@${VmIp}" "sudo mkdir -p $RemotePath/data && sudo chown -R `$USER:`$USER $RemotePath"

Write-Host "==> Copiando proyecto (sin data/ - la BD de la VM se conserva)..."
$staging = Join-Path $env:TEMP "ainvestor-deploy-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    # /XD data: no sobrescribir ainvestor.db remoto con copia local
    robocopy $LocalPath $staging /E /XD data .git __pycache__ .pytest_cache node_modules /XF *.pyc /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy falló con código $LASTEXITCODE"
    }
    scp @sshArgs -r "$staging\*" "${SshUser}@${VmIp}:${RemotePath}/"
}
finally {
    if (Test-Path $staging) {
        Remove-Item -Recurse -Force $staging
    }
}

if (Test-Path "$LocalPath\.env") {
    Write-Host "==> Copiando .env..."
    scp @sshArgs "$LocalPath\.env" "${SshUser}@${VmIp}:${RemotePath}/.env"
}

Write-Host "==> Ejecutando setup..."
ssh @sshArgs "${SshUser}@${VmIp}" "cd $RemotePath && sed -i 's/\r$//' deploy/gcp/setup.sh deploy/gcp/startup-script.sh 2>/dev/null; chmod +x deploy/gcp/setup.sh && bash deploy/gcp/setup.sh"

Write-Host ""
Write-Host "============================================"
Write-Host " URL: http://${VmIp}:8000"
Write-Host " Health: http://${VmIp}:8000/health"
Write-Host "============================================"
