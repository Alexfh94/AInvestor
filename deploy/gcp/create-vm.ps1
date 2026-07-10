# Crea VM e2-micro Always Free en GCP (us-east1) y despliega AInvestor
param(
    [string]$ProjectId = "regal-thought-457507-m4",
    [string]$Zone = "us-east1-b",
    [string]$InstanceName = "ainvestor",
    [string]$SshUser = $env:USERNAME,
    [string]$SshPubKeyPath = "$env:USERPROFILE\.ssh\id_ed25519.pub"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent | Split-Path -Parent

$gcloud = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
if (-not (Test-Path $gcloud)) { $gcloud = "gcloud" }
$env:PATH = "$(Split-Path $gcloud);$env:PATH"

function Ensure-Gcloud {
    if (Get-Command gcloud -ErrorAction SilentlyContinue) { return }
    Write-Host "Instalando Google Cloud SDK (winget)..."
    winget install Google.CloudSDK --accept-package-agreements --accept-source-agreements
    $gcloud = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    if (Test-Path $gcloud) { $env:PATH = "$(Split-Path $gcloud);$env:PATH" }
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        throw "gcloud no encontrado tras instalacion. Reinicia terminal o instala manualmente."
    }
}

Ensure-Gcloud

$account = gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null
if (-not $account) {
    Write-Host "Iniciando login de Google Cloud (se abrira el navegador)..."
    gcloud auth login
}

if (-not $ProjectId) {
    $ProjectId = gcloud config get-value project 2>$null
}
if (-not $ProjectId -or $ProjectId -eq "(unset)") {
    Write-Host "Creando proyecto ainvestor-bot..."
    $ProjectId = "ainvestor-bot-$(Get-Random -Maximum 99999)"
    gcloud projects create $ProjectId --name="AInvestor"
    gcloud config set project $ProjectId
}

Write-Host "Proyecto: $ProjectId"
gcloud config set project $ProjectId | Out-Null

Write-Host "Habilitando Compute Engine API..."
gcloud services enable compute.googleapis.com --project $ProjectId

$fwName = "ainvestor-allow-app"
$fwExists = gcloud compute firewall-rules describe $fwName --project $ProjectId 2>$null
if (-not $fwExists) {
    gcloud compute firewall-rules create $fwName `
        --project $ProjectId `
        --direction=INGRESS `
        --priority=1000 `
        --network=default `
        --action=ALLOW `
        --rules=tcp:22,tcp:8000 `
        --source-ranges=0.0.0.0/0 `
        --target-tags=ainvestor
}

$pubKey = (Get-Content $SshPubKeyPath -Raw).Trim()
$metaFile = Join-Path $env:TEMP "gcp-ainvestor-ssh-keys"
"${SshUser}:$pubKey" | Set-Content $metaFile -Encoding ascii -NoNewline

$startup = Join-Path $PSScriptRoot "startup-script.sh"

$exists = gcloud compute instances describe $InstanceName --zone $Zone --project $ProjectId 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "La instancia $InstanceName ya existe."
} else {
    Write-Host "Creando e2-micro en $Zone..."
    gcloud compute instances create $InstanceName `
        --project=$ProjectId `
        --zone=$Zone `
        --machine-type=e2-micro `
        --image-family=debian-12 `
        --image-project=debian-cloud `
        --boot-disk-size=30GB `
        --boot-disk-type=pd-standard `
        --tags=ainvestor,http-server `
        --metadata-from-file=ssh-keys=$metaFile,startup-script=$startup `
        --scopes=default,cloud-platform
}

$ip = gcloud compute instances describe $InstanceName --zone $Zone --project $ProjectId --format="get(networkInterfaces[0].accessConfigs[0].natIP)"
Write-Host "IP publica: $ip"

Write-Host "Esperando SSH (startup script)..."
Start-Sleep -Seconds 45

$deploy = Join-Path $PSScriptRoot "deploy-from-windows.ps1"
& $deploy -VmIp $ip -SshUser $SshUser

Write-Host "GCP_PROJECT=$ProjectId" | Out-Null
@(
    "GCP_PROJECT=$ProjectId",
    "GCP_IP=$ip",
    "GCP_ZONE=$Zone"
) | Set-Content (Join-Path $PSScriptRoot "last-deploy.env") -Encoding utf8
