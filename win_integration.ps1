# Wake System - Windows integration (status / setup / register-task / battery / boot)
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('status','setup','register-task','battery','boot')]
  [string]$Action,
  [string]$OutFile
)
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $AppDir 'config.json'
$DayNames = @('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')

function Emit([string]$Text) {
  # Elevated runs cannot use Start-Process -RedirectStandardOutput, so the
  # caller passes -OutFile instead and we write the result there too.
  if ($OutFile) { Set-Content -LiteralPath $OutFile -Value $Text -Encoding UTF8 }
  Write-Output $Text
}

function Read-Json([string]$p) {
  if (Test-Path -LiteralPath $p) {
    try { return Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
  }
  return $null
}

function Test-IsAdmin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pr = New-Object Security.Principal.WindowsPrincipal($id)
  return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Register-WakeTask {
  $cfg = Read-Json $ConfigPath
  if (-not $cfg) { throw 'config.json missing' }
  $paths = Read-Json (Join-Path $AppDir '_paths.json')
  if (-not $paths) { throw '_paths.json missing - start the alarm app once first' }
  $t = [DateTime]::ParseExact([string]$cfg.alarm.time, 'HH:mm', $null)
  $days = @($cfg.alarm.days | ForEach-Object { [int]$_ })
  if ($days.Count -eq 0) { throw 'alarm has no days selected' }
  $action = New-ScheduledTaskAction -Execute $paths.pythonw -Argument ('"{0}"' -f $paths.wake_task)
  if ($days.Count -ge 7) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $t
  } else {
    $dows = @($days | ForEach-Object { $DayNames[$_] })
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dows -At $t
  }
  $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName 'WakeAlarm' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
}

function Get-Status {
  $out = @{}
  $out.admin = Test-IsAdmin
  $startup = $false
  try {
    $props = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction Stop
    if ($null -ne $props.PSObject.Properties['WakeAlarm']) { $startup = $true }
  } catch { }
  $out.startup = $startup
  $task = @{ exists = $false; wake = $false; state = 'missing'; next = $null }
  try {
    $t = Get-ScheduledTask -TaskName 'WakeAlarm' -ErrorAction Stop
    $task.exists = $true
    $task.wake = [bool]$t.Settings.WakeToRun
    $task.state = [string]$t.State
    try {
      $i = Get-ScheduledTaskInfo -TaskName 'WakeAlarm' -ErrorAction Stop
      if ($i.NextRunTime) { $task.next = $i.NextRunTime.ToString('o') }
      $task.last_result = [int]$i.LastTaskResult
      if ($i.LastRunTime -and $i.LastRunTime -gt [DateTime]::MinValue) {
        $task.last_run = $i.LastRunTime.ToString('o')
      } else { $task.last_run = $null }
    } catch { }
  } catch { }
  $out.task = $task
  # Lid action is a hidden setting on this scheme, so /q never prints it;
  # read the value straight from the power registry instead.
  $lidAc = 'unknown'; $lidDc = 'unknown'
  try {
    $active = (& powercfg /getactivescheme | Out-String)
    $sm = [regex]::Match($active, 'GUID:\s*([0-9a-fA-F-]+)')
    if ($sm.Success) {
      $p = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\' +
           $sm.Groups[1].Value + '\4f971e89-eebd-4455-a8de-9e59040e7347\5ca83367-6e45-459f-a27b-476b1d01c936'
      if (Test-Path -LiteralPath $p) {
        $v = Get-ItemProperty -LiteralPath $p
        if ($null -ne $v.ACSettingIndex) { $lidAc = [int]$v.ACSettingIndex }
        if ($null -ne $v.DCSettingIndex) { $lidDc = [int]$v.DCSettingIndex }
      }
    }
  } catch { }
  $out.lid_ac = $lidAc
  $out.lid_dc = $lidDc
  $wtAc = 'unknown'; $wtDc = 'unknown'
  try {
    $q = (& powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE 2>&1 | Out-String)
    if ($q -match 'Current AC Power Setting Index:\s*0x([0-9A-Fa-f]+)') { $wtAc = [Convert]::ToInt32($Matches[1], 16) }
    if ($q -match 'Current DC Power Setting Index:\s*0x([0-9A-Fa-f]+)') { $wtDc = [Convert]::ToInt32($Matches[1], 16) }
  } catch { }
  $out.wake_timers_ac = $wtAc
  $out.wake_timers_dc = $wtDc
  return ($out | ConvertTo-Json -Compress -Depth 4)
}

function Invoke-Setup {
  $lines = @()
  if (-not (Test-IsAdmin)) { throw 'setup requires administrator rights' }
  & powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
  & powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
  $lines += 'Wake timers: enabled (plugged in + battery)'
  & powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
  & powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
  $lines += 'Lid close (plugged in + battery): do nothing (PC keeps running)'
  & powercfg /setactive SCHEME_CURRENT | Out-Null
  $paths = Read-Json (Join-Path $AppDir '_paths.json')
  if (-not $paths) { throw '_paths.json missing - start the app once, then run setup' }
  $runVal = '"{0}" "{1}"' -f $paths.pythonw, $paths.server
  Set-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'WakeAlarm' -Value $runVal
  $lines += 'Start with Windows: enabled'
  Register-WakeTask
  $cfg = Read-Json $ConfigPath
  $lines += ('Scheduled wake task: registered for ' + $cfg.alarm.time)
  # Decisive proof (admin only): which wake timers Windows has actually armed.
  try {
    $wt = (& powercfg /waketimers 2>&1 | Out-String).Trim()
    if ($wt -and $wt -notmatch 'no active wake timers') {
      $lines += 'Armed wake timers (powercfg /waketimers):'
      foreach ($l in ($wt -split "`r?`n")) { if ($l.Trim()) { $lines += '  ' + $l.Trim() } }
    } else {
      $lines += 'Armed wake timers: none active right now (normal - Windows arms them closer to the run time)'
    }
  } catch {
    $lines += ('Armed wake timers: check failed - ' + $_.Exception.Message)
  }
  return (@{ ok = $true; output = $lines } | ConvertTo-Json -Compress -Depth 4)
}

switch ($Action) {
  'status' { Write-Output (Get-Status) }
  'setup' {
    try { Emit (Invoke-Setup) }
    catch {
      Emit (@{ ok = $false; output = @("ERROR: $($_.Exception.Message)") } | ConvertTo-Json -Compress -Depth 4)
      exit 1
    }
  }
  'register-task' {
    try { Register-WakeTask; Write-Output (@{ ok = $true } | ConvertTo-Json -Compress) }
    catch {
      Write-Output (@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress)
      exit 1
    }
  }
  'battery' {
    $b = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $b) { Write-Output 'none' }
    else { Write-Output ('{0}|{1}' -f [int]$b.EstimatedChargeRemaining, [int]$b.BatteryStatus) }
  }
  'boot' {
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Output $os.LastBootUpTime.ToUniversalTime().ToString('o')
  }
}
