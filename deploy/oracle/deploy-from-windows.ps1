# Despliegue AInvestor a Oracle VM desde Windows
param(
    [Parameter(Mandatory=$true)][string]$VmIp,
    [string]$SshKey = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$LocalPath = "C:\Users\alexf\Desktop\AInvestor",
    [string]$RemotePath = "/opt/ainvestor"
)

$ErrorActionPreference = "Stop"
$user = "ubuntu"

Write-Host "==> Comprobando SSH a ${user}@${VmIp}..."
ssh -i $SshKey -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "${user}@${VmIp}" "echo ok"

Write-Host "==> Creando directorio remoto..."
ssh -i $SshKey "${user}@${VmIp}" "sudo mkdir -p $RemotePath && sudo chown $user:$user $RemotePath"

Write-Host "==> Copiando proyecto (puede tardar unos minutos)..."
scp -i $SshKey -r "$LocalPath\*" "${user}@${VmIp}:${RemotePath}/"

Write-Host "==> Arrancando Docker..."
$remoteCmd = @"
cd $RemotePath
mkdir -p data
if [ ! -f .env ]; then cp .env.example .env; fi
docker compose up -d --build
curl -s http://localhost:8000/health || true
"@
ssh -i $SshKey "${user}@${VmIp}" $remoteCmd

Write-Host ""
Write-Host "============================================"
Write-Host " URL: http://${VmIp}:8000"
Write-Host " Health: http://${VmIp}:8000/health"
Write-Host "============================================"
