<#
.SYNOPSIS
    Laptop Diagnostics Self-Registering Agent (v2.1)
    Zero dependency — uses only built-in Windows components.

.DESCRIPTION
    Collects hardware metrics from this laptop using native Windows WMI,
    registers itself with the Diagnostics Server automatically on first run,
    and reports metrics every 10 minutes.

    NO INSTALLATION REQUIRED.
    NO PYTHON. NO THIRD-PARTY SOFTWARE. NO ADMIN RIGHTS FOR BASIC USE.
    (Admin rights only needed for Task Scheduler auto-start setup.)

    Sensor sources (all native Windows, no third-party tool needed):
      CPU Usage    — Win32_Processor             (always available)
      CPU Temp     — MSAcpi_ThermalZoneTemperature (available on most laptops)
      RAM          — Win32_OperatingSystem         (always available)
      Disk         — Win32_LogicalDisk             (always available)
      Model        — Win32_ComputerSystem          (always available)
      MAC/ID       — Win32_NetworkAdapterConfiguration (always available)
      Fan/Voltages — LibreHardwareMonitor WMI      (if LHM is running, optional)
                     OpenHardwareMonitor WMI       (if OHM is running, optional)

    If LHM/OHM is not running, voltage and fan values fall back to safe
    defaults. The Sugeno FIS on the server still runs diagnostics using
    CPU temperature and usage — the most reliable fault indicators.

.PARAMETER ServerUrl
    URL of the Diagnostics Server. Can also be set via DIAG_SERVER_URL env var.

.PARAMETER ApiKey
    Agent API key (must match AGENT_API_KEY on server). 
    Can also be set via DIAG_API_KEY env var.

.PARAMETER IntervalMinutes
    How often to report (default 10). Can be set via DIAG_INTERVAL_MIN env var.

.PARAMETER Category
    Laptop category: basic|midrange|highend|gaming|workstation (default: midrange)

.PARAMETER AlertEmail
    Email address for fault alerts on this laptop.

.PARAMETER DisplayName
    Override the display name (default: computer hostname).

.PARAMETER Once
    Send one report then exit.

.PARAMETER Test
    Print sensor readings to console, do not send to server.

.PARAMETER Install
    Register this script as a Windows Task Scheduler task (runs at startup).
    Requires Administrator rights.

.PARAMETER Uninstall
    Remove the scheduled task.
#>

[CmdletBinding()]
param(
    [string]$ServerUrl      = $env:DIAG_SERVER_URL,
    [string]$ApiKey         = $env:DIAG_API_KEY,
    [int]$IntervalMinutes   = $(if ($env:DIAG_INTERVAL_MIN) { [int]$env:DIAG_INTERVAL_MIN } else { 10 }),
    [string]$Category       = $(if ($env:DIAG_CATEGORY) { $env:DIAG_CATEGORY } else { "midrange" }),
    [string]$AlertEmail     = $(if ($env:DIAG_EMAIL) { $env:DIAG_EMAIL } else { "" }),
    [string]$DisplayName    = $(if ($env:DIAG_NAME) { $env:DIAG_NAME } else { "" }),
    [switch]$Once,
    [switch]$Test,
    [switch]$Install,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ── Constants ─────────────────────────────────────────────────────────────────
$TASK_NAME  = "LaptopDiagnosticsAgent"
$STATE_FILE = Join-Path $PSScriptRoot ".agent_state.json"
$LOG_FILE   = Join-Path $PSScriptRoot "laptop_agent.log"
$VERSION    = "2.1"

# Sensor defaults used when native WMI cannot read a value
$DEFAULTS = @{
    cpu_temp    = 60.0
    fan_rpm     = 2500.0
    cpu_voltage = 1.20
    ram_voltage = 1.25
    gpu_voltage = 1.00
    rail_3v3    = 3.30
    rail_5v_mw  = 5000.0
}


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts   = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "$ts [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}


# ─────────────────────────────────────────────────────────────────────────────
# Machine Identity
# ─────────────────────────────────────────────────────────────────────────────

function Get-MachineId {
    try {
        # Get first physical (non-virtual, non-loopback) MAC
        $mac = Get-WmiObject -Class Win32_NetworkAdapterConfiguration `
                    -Filter "IPEnabled=True" -ErrorAction Stop |
               Where-Object { $_.MACAddress -and $_.MACAddress -ne "00:00:00:00:00:00" } |
               Select-Object -ExpandProperty MACAddress -First 1

        if (-not $mac) { $mac = $env:COMPUTERNAME }
        $raw    = "$mac`:$env:COMPUTERNAME"
        $bytes  = [System.Text.Encoding]::UTF8.GetBytes($raw)
        $sha1   = [System.Security.Cryptography.SHA1]::Create()
        $hash   = $sha1.ComputeHash($bytes)
        return  ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
    }
    catch {
        # Absolute fallback — hash of hostname alone
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($env:COMPUTERNAME)
        $sha1  = [System.Security.Cryptography.SHA1]::Create()
        $hash  = $sha1.ComputeHash($bytes)
        return ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
    }
}

function Get-LaptopModel {
    try {
        $model = (Get-WmiObject -Class Win32_ComputerSystem -ErrorAction Stop).Model
        if ($model -and $model -notmatch "System Product Name|To Be Filled") {
            return $model.Trim()
        }
        $board = (Get-WmiObject -Class Win32_BaseBoard -ErrorAction Stop).Product
        if ($board) { return $board.Trim() }
    } catch {}
    return "Windows Machine"
}

function Get-DisplayName {
    if ($DisplayName) { return $DisplayName }
    return $env:COMPUTERNAME
}


# ─────────────────────────────────────────────────────────────────────────────
# Sensor Collection — all native WMI, no third-party tools required
# ─────────────────────────────────────────────────────────────────────────────

function Get-CpuUsage {
    try {
        $cpu = Get-WmiObject -Class Win32_Processor -ErrorAction Stop
        # Average across all sockets
        $avg = ($cpu | Measure-Object -Property LoadPercentage -Average).Average
        return [Math]::Round($avg, 2)
    } catch { return 50.0 }
}

function Get-CpuTempNative {
    try {
        $zones = Get-WmiObject -Namespace "root\WMI" `
                     -Class MSAcpi_ThermalZoneTemperature `
                     -ErrorAction Stop
        if ($zones) {
            # Prefer the highest zone temperature (usually the CPU package)
            $maxDeciK = ($zones | Measure-Object -Property CurrentTemperature -Maximum).Maximum
            $celsius  = [Math]::Round(($maxDeciK / 10.0) - 273.15, 1)
            # Sanity check — plausible laptop CPU temp (ge 20, le 120)
            if ($celsius -ge 20 -and $celsius -le 120) {
                return $celsius
            }
        }
    } catch {}
    return $null
}

function Get-RamInfo {
    try {
        $os = Get-WmiObject -Class Win32_OperatingSystem -ErrorAction Stop
        $totalKB = $os.TotalVisibleMemorySize
        $freeKB  = $os.FreePhysicalMemory
        $usedKB  = $totalKB - $freeKB
        return @{
            ram_percent  = [Math]::Round(($usedKB / $totalKB) * 100, 2)
            ram_total_gb = [Math]::Round($totalKB / 1MB, 2)
            ram_used_gb  = [Math]::Round($usedKB  / 1MB, 2)
        }
    } catch {
        return @{ ram_percent = 50.0; ram_total_gb = 8.0; ram_used_gb = 4.0 }
    }
}

function Get-DiskPercent {
    try {
        $sysDrive = ($env:SystemDrive ?? "C:")
        $disk = Get-WmiObject -Class Win32_LogicalDisk `
                    -Filter "DeviceID='$sysDrive'" -ErrorAction Stop
        if ($disk -and $disk.Size -gt 0) {
            return [Math]::Round((($disk.Size - $disk.FreeSpace) / $disk.Size) * 100, 2)
        }
    } catch {}
    return 0.0
}

function Get-HardwareSensors {
    $result = $DEFAULTS.Clone()
    $result["_source"] = "defaults"

    foreach ($ns in @("root\LibreHardwareMonitor", "root\OpenHardwareMonitor")) {
        try {
            $sensors = Get-WmiObject -Namespace $ns -Class Sensor -ErrorAction Stop
            if (-not $sensors) { continue }

            foreach ($s in $sensors) {
                $name  = ($s.Name  ?? "").ToLower()
                $stype = ($s.SensorType ?? "").ToLower()
                $val   = [double]($s.Value ?? 0)

                switch ($stype) {
                    "temperature" {
                        if (($name -match "cpu|core|package|tdie") -and
                            ($result["cpu_temp"] -eq $DEFAULTS["cpu_temp"])) {
                            $result["cpu_temp"] = [Math]::Round($val, 1)
                        }
                    }
                    "fan" {
                        if ($val -gt 0 -and (
                            ($name -match "cpu") -or
                            ($result["fan_rpm"] -eq $DEFAULTS["fan_rpm"]))) {
                            $result["fan_rpm"] = [Math]::Round($val, 0)
                        }
                    }
                    "voltage" {
                        if ($name -match "vcore|cpu.*core") {
                            if ($val -ge 0.5 -and $val -le 2.0) {
                                $result["cpu_voltage"] = [Math]::Round($val, 3)
                            }
                        }
                        elseif ($name -match "dimm|ram|dram|memory") {
                            $result["ram_voltage"] = [Math]::Round($val, 3)
                        }
                        elseif ($name -match "gpu") {
                            $result["gpu_voltage"] = [Math]::Round($val, 3)
                        }
                        elseif ($name -match "3\.3|3v3") {
                            $result["rail_3v3"] = [Math]::Round($val, 3)
                        }
                        elseif ($name -match "5v|\+5") {
                            # Fixed 5V scaling from x100 to x1000
                            $result["rail_5v_mw"] = [Math]::Min([Math]::Round($val * 1000, 1), 10000)
                        }
                    }
                }
            }

            # If we got at least cpu_temp from a real sensor, mark source and stop
            if ($result["cpu_temp"] -ne $DEFAULTS["cpu_temp"]) {
                $result["_source"] = $ns
                break
            }
        }
        catch { <# namespace not available, try next #> }
    }
    return $result
}

function Collect-Metrics {
    $metrics = @{}

    # Core metrics — always available
    $metrics["cpu_usage"]    = Get-CpuUsage
    $metrics["disk_percent"] = Get-DiskPercent

    $ram = Get-RamInfo
    $metrics["ram_percent"]  = $ram["ram_percent"]
    $metrics["ram_total_gb"] = $ram["ram_total_gb"]
    $metrics["ram_used_gb"]  = $ram["ram_used_gb"]

    # Hardware sensors
    $hw = Get-HardwareSensors
    $metrics["fan_rpm"]      = $hw["fan_rpm"]
    $metrics["cpu_voltage"]  = $hw["cpu_voltage"]
    $metrics["ram_voltage"]  = $hw["ram_voltage"]
    $metrics["gpu_voltage"]  = $hw["gpu_voltage"]
    $metrics["rail_3v3"]     = $hw["rail_3v3"]
    $metrics["rail_5v_mw"]   = $hw["rail_5v_mw"]

    # CPU Temperature: prefer LHM/OHM result, fall back to native WMI ACPI
    if ($hw["cpu_temp"] -ne $DEFAULTS["cpu_temp"]) {
        $metrics["cpu_temp"] = $hw["cpu_temp"]
    }
    else {
        $nativeTemp = Get-CpuTempNative
        $metrics["cpu_temp"] = if ($nativeTemp -ne $null) { $nativeTemp } else { $DEFAULTS["cpu_temp"] }
    }

    # Metadata
    $metrics["platform"]  = "Windows"
    $metrics["hostname"]  = $env:COMPUTERNAME
    $metrics["timestamp"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

    return $metrics
}


# ─────────────────────────────────────────────────────────────────────────────
# Local State (remembers laptop_id across restarts)
# ─────────────────────────────────────────────────────────────────────────────

function Load-State {
    if (Test-Path $STATE_FILE) {
        try {
            return Get-Content $STATE_FILE -Raw | ConvertFrom-Json
        } 
        catch {
            Write-Log "Corrupted state file found. Clearing and resetting registration." "WARN"
            Remove-Item $STATE_FILE -ErrorAction SilentlyContinue
        }
    }
    return $null
}

function Save-State {
    param($State)
    try {
        $State | ConvertTo-Json | Set-Content $STATE_FILE -Encoding UTF8
    } catch {
        Write-Log "Could not save state file: $_" "WARN"
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

function Invoke-DiagApi {
    param(
        [string]$Endpoint,
        [hashtable]$Body,
        [int]$MaxRetries = 3
    )
    $url     = $ServerUrl.TrimEnd("/") + $Endpoint
    $headers = @{
        "Content-Type" = "application/json"
        "X-Agent-Key"  = $ApiKey
    }
    $json = $Body | ConvertTo-Json -Depth 10 -Compress

    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $response = Invoke-RestMethod `
                -Uri $url `
                -Method Post `
                -Headers $headers `
                -Body $json `
                -TimeoutSec 30 `
                -ErrorAction Stop

            return $response
        }
        catch [System.Net.WebException] {
            $statusCode = [int]$_.Exception.Response.StatusCode
            if ($statusCode -eq 401) {
                Write-Log "API key rejected (401). Check DIAG_API_KEY matches AGENT_API_KEY on server." "ERROR"
                return $null
            }
            if ($statusCode -eq 404) {
                Write-Log "Endpoint not found (404): $Endpoint" "ERROR"
                return $null
            }
            Write-Log "Attempt $attempt/$MaxRetries failed (HTTP $statusCode): $_" "WARN"
        }
        catch {
            Write-Log "Attempt $attempt/$MaxRetries failed: $_" "WARN"
        }
        if ($attempt -lt $MaxRetries) { Start-Sleep -Seconds 10 }
    }
    return $null
}


# ─────────────────────────────────────────────────────────────────────────────
# Auto Registration
# ─────────────────────────────────────────────────────────────────────────────

function Register-OrLoad {
    # Try loading saved state first
    $state = Load-State
    if ($state -and $state.laptop_id -and $state.server_url -eq $ServerUrl) {
        Write-Log "Using saved laptop ID: $($state.laptop_id)  (name: $($state.name))"
        return $state.laptop_id
    }

    Write-Log "No saved registration — registering with $ServerUrl ..."

    $body = @{
        machine_id         = Get-MachineId
        name               = Get-DisplayName
        model              = Get-LaptopModel
        category           = $Category
        email              = $AlertEmail
        platform           = "Windows"
        hostname           = $env:COMPUTERNAME
        polling_interval   = ($IntervalMinutes * 60)
    }

    $result = Invoke-DiagApi -Endpoint "/api/agent/register" -Body $body
    if (-not $result) {
        Write-Log "Registration failed. Check DIAG_SERVER_URL and DIAG_API_KEY." "ERROR"
        exit 1
    }

    $action = if ($result.existing) { "Re-connected to" } else { "Registered as" }
    Write-Log "$action '$($result.name)'  (ID: $($result.laptop_id))"

    $state = @{
        laptop_id  = $result.laptop_id
        name       = $result.name
        server_url = $ServerUrl
        registered = (Get-Date).ToUniversalTime().ToString("o")
    }
    Save-State $state
    return $result.laptop_id
}


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

function Send-Report {
    param([string]$LaptopId, [hashtable]$Metrics)

    $body = @{
        laptop_id = $LaptopId
        metrics   = $Metrics
    }

    $result = Invoke-DiagApi -Endpoint "/api/agent/report" -Body $body
    if ($result) {
        $conf = [Math]::Round($result.confidence * 100, 1)
        Write-Log ("Diagnosis: {0,-20} | Severity: {1,-8} | Confidence: {2}%" -f `
                    $result.diagnosis, $result.severity, $conf)
        if ($result.notified) {
            Write-Log "Alert email sent for this report."
        }
        return $true
    }
    else {
        Write-Log "Report failed — will retry next interval." "WARN"
        return $false
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Task Scheduler install / uninstall
# ─────────────────────────────────────────────────────────────────────────────

function Install-ScheduledTask {
    Write-Log "Installing Windows Scheduled Task '$TASK_NAME' ..."

    $psArgs = "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
              "-File `"$PSCommandPath`""

    if ($ServerUrl)      { $psArgs += " -ServerUrl `"$ServerUrl`"" }
    if ($ApiKey)         { $psArgs += " -ApiKey `"$ApiKey`"" }
    if ($AlertEmail)     { $psArgs += " -AlertEmail `"$AlertEmail`"" }
    if ($DisplayName)    { $psArgs += " -DisplayName `"$DisplayName`"" }
    if ($Category)       { $psArgs += " -Category `"$Category`"" }
    $psArgs += " -IntervalMinutes $IntervalMinutes"

    $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $repeat  = New-TimeSpan -Minutes $IntervalMinutes
    $trigger.RepetitionInterval = $repeat
    $trigger.RepetitionDuration = [System.TimeSpan]::MaxValue

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes ($IntervalMinutes - 1))

    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $TASK_NAME `
        -Action   $action `
        -Trigger  $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    Write-Log "Task '$TASK_NAME' installed. It will run at startup and every $IntervalMinutes minutes."
}

function Uninstall-ScheduledTask {
    try {
        Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction Stop
        Write-Log "Task '$TASK_NAME' removed."
    } catch {
        Write-Log "Task '$TASK_NAME' not found or already removed." "WARN"
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if ($Uninstall) {
    Uninstall-ScheduledTask
    exit 0
}

if ($Install) {
    if (-not $ServerUrl) { Write-Log "Pass -ServerUrl or set DIAG_SERVER_URL" "ERROR"; exit 1 }
    if (-not $ApiKey)    { Write-Log "Pass -ApiKey or set DIAG_API_KEY" "ERROR"; exit 1 }
    Install-ScheduledTask
    exit 0
}

if ($Test) {
    Write-Host "`n=== Sensor Readings (no data sent) ===" -ForegroundColor Cyan
    $m = Collect-Metrics
    foreach ($key in ($m.Keys | Sort-Object)) {
        Write-Host ("  {0,-20}: {1}" -f $key, $m[$key])
    }
    Write-Host "`nNote: If voltages/fan show defaults, run Libre Hardware Monitor as Administrator." -ForegroundColor Yellow
    Write-Host "CPU temp via native WMI ACPI: $(Get-CpuTempNative) °C" -ForegroundColor Gray
    exit 0
}

if (-not $ServerUrl) {
    Write-Log "DIAG_SERVER_URL not set. Set env var or pass -ServerUrl." "ERROR"
    exit 1
}
if (-not $ApiKey) {
    Write-Log "DIAG_API_KEY not set. Set env var or pass -ApiKey." "ERROR"
    exit 1
}

Write-Log ("=" * 55)
Write-Log "  Laptop Diagnostics Agent v$VERSION"
Write-Log "  Server  : $ServerUrl"
Write-Log "  Hostname: $env:COMPUTERNAME"
Write-Log  "  Model   : $(Get-LaptopModel)"
Write-Log "  Interval: $IntervalMinutes minutes"
Write-Log ("=" * 55)

$laptopId = Register-OrLoad

if ($Once) {
    $metrics = Collect-Metrics
    Send-Report -LaptopId $laptopId -Metrics $metrics
    exit 0
}

Write-Log "Agent running. Reporting every $IntervalMinutes minutes. Press Ctrl+C to stop."
while ($true) {
    try {
        $laptopId = Register-OrLoad
        $metrics  = Collect-Metrics
        Send-Report -LaptopId $laptopId -Metrics $metrics
    }
    catch {
        Write-Log "Unexpected error: $_" "ERROR"
    }
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
