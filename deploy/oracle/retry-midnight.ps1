# Reintenta crear la VM Always Free en Oracle (Madrid) desde las 00:00 hora España.
# Uso:
#   .\deploy\oracle\retry-midnight.ps1              # espera a las 00:00 y reintenta
#   .\deploy\oracle\retry-midnight.ps1 -StartNow  # reintenta ya (sin esperar)
#   .\deploy\oracle\retry-midnight.ps1 -RegisterTask  # programa tarea Windows para hoy a las 00:00

param(
    [switch]$StartNow,
    [switch]$RegisterTask,
    [int]$IntervalMinutes = 5,
    [int]$MaxAttempts = 120,
    [string]$InstanceName = "ainvestor",
    [string]$Shape = "VM.Standard.A1.Flex",
    [string]$SshPubKeyPath = "$PSScriptRoot\ssh_public_key.pub"
)

$ErrorActionPreference = "Stop"
$LogDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("retry-{0:yyyyMMdd}.log" -f (Get-Date))

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Get-SpainNow {
    return [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date).ToUniversalTime(), "Romance Standard Time")
}

function Wait-UntilSpainMidnight {
    $now = Get-SpainNow
    $midnight = $now.Date.AddDays(1)
    if ($now.Hour -eq 0 -and $now.Minute -lt 5) {
        Write-Log "Ya estamos en la franja de medianoche ($($now.ToString('HH:mm')))."
        return
    }
    $wait = $midnight - $now
    Write-Log "Esperando hasta las 00:00 (España). Ahora: $($now.ToString('HH:mm:ss')). Quedan $($wait.ToString('hh\:mm\:ss'))."
    Start-Sleep -Seconds [int]$wait.TotalSeconds
    Write-Log "00:00 alcanzado - iniciando reintentos."
}

function Ensure-OciCli {
    if (Get-Command oci -ErrorAction SilentlyContinue) { return }
    Write-Log "Instalando OCI CLI (pip)..."
    python3 -m pip install oci-cli -q
    if (-not (Get-Command oci -ErrorAction SilentlyContinue)) {
        throw "No se pudo instalar oci-cli. Ejecuta: python3 -m pip install oci-cli"
    }
}

function Test-OciConfig {
    $config = Join-Path $env:USERPROFILE ".oci\config"
    if (-not (Test-Path $config)) {
        Write-Log 'FALTA ~/.oci/config - ejecuta primero: deploy\oracle\setup-oci-api.ps1'
        return $false
    }
    return $true
}

function Invoke-CreateInstance {
    Ensure-OciCli
    if (-not (Test-OciConfig)) { return $null }

    $region = "eu-madrid-1"
    $compartment = oci iam compartment list --all --query "data[0].id" --raw-output 2>$null
    if (-not $compartment) { Write-Log "No se pudo obtener compartment-id"; return $null }
    $ad = (oci iam availability-domain list --query "data[0].name" --raw-output --region $region)
    $image = (oci compute image list --compartment-id $compartment --operating-system "Canonical Ubuntu" --operating-system-version "24.04" --shape $Shape --query "data[0].id" --raw-output --region $region)
    if (-not $image) {
        $image = (oci compute image list --compartment-id $compartment --operating-system "Canonical Ubuntu" --shape $Shape --query "data[0].id" --raw-output --region $region)
    }
    $sshKey = Get-Content $SshPubKeyPath -Raw

    $metadata = @{ ssh_authorized_keys = $sshKey.Trim() } | ConvertTo-Json -Compress
    $shapeConfig = '{"ocpus":1,"memoryInGBs":6}'
    if ($Shape -eq "VM.Standard.E2.1.Micro") { $shapeConfig = $null }

    $args = @(
        "compute", "instance", "launch",
        "--availability-domain", $ad,
        "--compartment-id", $compartment,
        "--display-name", $InstanceName,
        "--image-id", $image,
        "--shape", $Shape,
        "--assign-public-ip", "true",
        "--metadata", $metadata,
        "--region", $region,
        "--wait-for-state", "RUNNING",
        "--max-wait-seconds", "600"
    )
    if ($shapeConfig) { $args += @("--shape-config", $shapeConfig) }

    Write-Log "Lanzando instancia ($Shape) en $ad..."
    $out = & oci @args 2>&1
    $text = $out | Out-String
    if ($LASTEXITCODE -ne 0) {
        if ($text -match "Out of capacity") { Write-Log "Sin capacidad - reintentar mas tarde."; return $null }
        Write-Log "Error OCI: $text"
        return $null
    }
    $instanceId = oci compute instance list --compartment-id $compartment --display-name $InstanceName --query "data[0].id" --raw-output --region $region
    $ip = oci compute instance list-vnics --instance-id $instanceId --query 'data[0]."public-ip"' --raw-output --region $region
    Write-Log "Instancia creada. IP publica: $ip"
    return $ip
}

if ($RegisterTask) {
    $taskName = "AInvestor-Oracle-Retry"
    $script = (Resolve-Path $PSCommandPath).Path
    $nowSpain = Get-SpainNow
    $at = $nowSpain.Date
    if ($nowSpain.Hour -ge 1 -or ($nowSpain.Hour -eq 0 -and $nowSpain.Minute -gt 5)) { $at = $at.AddDays(1) }
    $trigger = New-ScheduledTaskTrigger -Once -At $at
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -StartNow"
    Register-ScheduledTask -TaskName $taskName -Trigger $trigger -Action $action -Force | Out-Null
    Write-Log "Tarea programada '$taskName' para $($at.ToString('yyyy-MM-dd HH:mm')) (hora España)."
    exit 0
}

if (-not $StartNow) { Wait-UntilSpainMidnight }

$shapes = @("VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro")
for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
  foreach ($s in $shapes) {
    $Shape = $s
    Write-Log "Intento $attempt/$MaxAttempts - forma $Shape"
    $ip = Invoke-CreateInstance
    if ($ip) {
      Write-Log "Desplegando AInvestor en $ip ..."
      & (Join-Path $PSScriptRoot "deploy-from-windows.ps1") -VmIp $ip
      Write-Log "Listo: http://${ip}:8000"
      exit 0
    }
  }
  Write-Log "Pausa $IntervalMinutes min antes del siguiente intento."
  Start-Sleep -Seconds ($IntervalMinutes * 60)
}

Write-Log "Agotados los intentos de esta noche."
exit 1
