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
ssh @sshArgs "${SshUser}@${VmIp}" "sudo mkdir -p $RemotePath && sudo chown `$USER:`$USER $RemotePath"

Write-Host "==> Copiando proyecto..."
scp @sshArgs -r "$LocalPath\*" "${SshUser}@${VmIp}:${RemotePath}/"

if (Test-Path "$LocalPath\.env") {
    Write-Host "==> Copiando .env..."
    scp @sshArgs "$LocalPath\.env" "${SshUser}@${VmIp}:${RemotePath}/.env"
}

Write-Host "==> Ejecutando setup..."
ssh @sshArgs "${SshUser}@${VmIp}" "cd $RemotePath && chmod +x deploy/gcp/setup.sh && bash deploy/gcp/setup.sh"

Write-Host ""
Write-Host "============================================"
Write-Host " URL: http://${VmIp}:8000"
Write-Host " Health: http://${VmIp}:8000/health"
Write-Host "============================================"
