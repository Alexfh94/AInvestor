# Configura OCI CLI para reintentos automáticos (una sola vez).
# 1. Genera clave API si no existe
# 2. Muestra la clave pública para pegarla en Oracle Console
# 3. Crea ~/.oci/config (pide OCIDs al usuario)

$ErrorActionPreference = "Stop"
$ociDir = Join-Path $env:USERPROFILE ".oci"
New-Item -ItemType Directory -Force -Path $ociDir | Out-Null

$openssl = "C:\Program Files\Git\usr\bin\openssl.exe"
$key = Join-Path $ociDir "oci_api_key.pem"
$pub = Join-Path $ociDir "oci_api_key_public.pem"

if (-not (Test-Path $key)) {
    if (-not (Test-Path $openssl)) { throw "Instala Git for Windows (openssl) o genera la clave manualmente." }
    & $openssl genrsa -out $key 2048
    & $openssl rsa -pubout -in $key -out $pub
    Write-Host "Clave API generada."
}

Write-Host ""
Write-Host "=== PASO 1: Añadir clave en Oracle ==="
Write-Host "Abre: https://cloud.oracle.com/identity/users?region=eu-madrid-1"
Write-Host "Tu usuario -> API Keys -> Add API Key -> Paste Public Key"
Write-Host "IMPORTANTE: pega TODO el bloque, incluyendo las lineas BEGIN y END."
Write-Host ""
Get-Content $pub -Raw
Write-Host ""
Write-Host "=== PASO 2: OCIDs (al añadir la clave Oracle muestra un dialogo de configuracion) ==="
Write-Host "Copia tenancy OCID, user OCID y fingerprint del dialogo."
Write-Host ""

$tenancy = Read-Host "Tenancy OCID"
$user = Read-Host "User OCID"
$fingerprint = Read-Host "Fingerprint"

$config = @"
[DEFAULT]
user=$user
fingerprint=$fingerprint
tenancy=$tenancy
region=eu-madrid-1
key_file=$key
"@
$config | Set-Content -Path (Join-Path $ociDir "config") -Encoding utf8
Write-Host ""
Write-Host "Config guardada en $ociDir\config"
Write-Host "Esta noche a las 00:00 ejecuta: .\deploy\oracle\retry-midnight.ps1"
Write-Host "O programa la tarea: .\deploy\oracle\retry-midnight.ps1 -RegisterTask"
