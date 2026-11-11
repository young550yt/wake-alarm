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
  # One scheduled task per armed alarm: 'WakeAlarm', 'WakeAlarm_2', ...
  $rows = @()
  if ($cfg.PSObject.Properties['alarms'] -and @($cfg.alarms).Count -gt 0) {
    foreach ($x in @($cfg.alarms)) {
      if (-not $x.enabled) { continue }
      $d = @($x.days | ForEach-Object { [int]$_ })
      if ($d.Count -eq 0) { continue }
      $rows += , @([string]$x.time, $d)
    }
  } else {
    $d = @($cfg.alarm.days | ForEach-Object { [int]$_ })
    if ($d.Count -gt 0) { $rows += , @([string]$cfg.alarm.time, $d) }
  }
  if ($rows.Count -eq 0) { throw 'no armed alarm with days selected' }
  $action = New-ScheduledTaskAction -Execute $paths.pythonw -Argument ('"{0}"' -f $paths.wake_task)
  $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  $names = @()
  for ($i = 0; $i -lt $rows.Count; $i++) {
    $name = if ($i -eq 0) { 'WakeAlarm' } else { 'WakeAlarm_' + ($i + 1) }
    $names += $name
    $t = [DateTime]::ParseExact($rows[$i][0], 'HH:mm', $null)
    $days = $rows[$i][1]
    if ($days.Count -ge 7) {
      $trigger = New-ScheduledTaskTrigger -Daily -At $t
    } else {
      $dows = @($days | ForEach-Object { $DayNames[$_] })
      $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dows -At $t
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  }
  # Drop tasks left over from a previous config (deleted/disarmed alarm)
  try {
    foreach ($old in @(Get-ScheduledTask -TaskName 'WakeAlarm*' -ErrorAction SilentlyContinue)) {
      if ($names -notcontains $old.TaskName) {
        Unregister-ScheduledTask -TaskName $old.TaskName -Confirm:$false
      }
    }
  } catch { }
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
  # All wake tasks (one per armed alarm); 'WakeAlarm' itself stays primary
  # for the existing UI contract.
  $tasks = @()
  try {
    $found = @(Get-ScheduledTask -TaskName 'WakeAlarm*' -ErrorAction SilentlyContinue)
    foreach ($st in ($found | Sort-Object TaskName)) {
      $info = $null
      try { $info = Get-ScheduledTaskInfo -TaskName $st.TaskName -ErrorAction Stop } catch { }
      $tasks += @{
        name = [string]$st.TaskName
        exists = $true
        wake = [bool]$st.Settings.WakeToRun
        state = [string]$st.State
        next = $(if ($info -and $info.NextRunTime) { $info.NextRunTime.ToString('o') } else { $null })
        last_result = $(if ($info) { [int]$info.LastTaskResult } else { $null })
        last_run = $(if ($info -and $info.LastRunTime -and $info.LastRunTime -gt [DateTime]::MinValue) { $info.LastRunTime.ToString('o') } else { $null })
      }
    }
  } catch { }
  $primary = $null
  foreach ($x in $tasks) { if ($x['name'] -eq 'WakeAlarm') { $primary = $x; break } }
  if (-not $primary -and $tasks.Count -gt 0) { $primary = $tasks[0] }
  $task = $primary
  if (-not $task) { $task = @{ exists = $false; wake = $false; state = 'missing'; next = $null } }
  $out.task = $task
  $out.tasks = $tasks
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
  $times = @()
  if ($cfg.PSObject.Properties['alarms'] -and @($cfg.alarms).Count -gt 0) {
    foreach ($x in @($cfg.alarms)) {
      if ($x.enabled -and @($x.days).Count -gt 0) { $times += [string]$x.time }
    }
  }
  if ($times.Count -eq 0 -and $cfg.alarm.enabled) { $times = @([string]$cfg.alarm.time) }
  if ($times.Count -eq 0) { $times = @([string]$cfg.alarm.time) }
  $lines += ('Scheduled wake task(s): registered for ' + ($times -join ', '))
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
