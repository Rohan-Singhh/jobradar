# One-shot Windows setup: venv, deps, config, and the two scheduled tasks.
# The Windows counterpart of install.sh. Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$Dir = $PSScriptRoot
Set-Location $Dir

# A Windows-native Python 3.11+. The py launcher goes first because a bare
# `python` on PATH is often an MSYS2 build, whose venv has no Scripts\ folder.
function Test-Python($exe, $pyArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @pyArgs -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

$candidates = @(
    @{ Exe = 'py'; Args = @('-3.13') },
    @{ Exe = 'py'; Args = @('-3.12') },
    @{ Exe = 'py'; Args = @('-3.11') },
    @{ Exe = 'python'; Args = @() }
)
$py = $candidates | Where-Object { Test-Python $_.Exe $_.Args } | Select-Object -First 1
if (-not $py) { throw 'Python 3.11+ not found. Install it from python.org and re-run.' }

Write-Host "==> creating virtualenv ($($py.Exe) $($py.Args -join ' '))"
$pyArgs = $py.Args
& $py.Exe @pyArgs -m venv .venv
if (-not (Test-Path .venv\Scripts\python.exe)) {
    throw '.venv\Scripts\python.exe missing - that Python is not a Windows build. Install Python from python.org.'
}
& .venv\Scripts\python.exe -m pip -q --disable-pip-version-check install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

if (-not (Test-Path config.yaml)) {
    Copy-Item config.example.yaml config.yaml
    Write-Host '==> created config.yaml - edit it'
}
if (-not (Test-Path run.sh)) {
    Copy-Item run.example.sh run.sh
    Write-Host '==> created run.sh - put your keys in it'
}

# Task Scheduler stands in for launchd. Tasks run run.sh through Git Bash, so
# the secrets live in run.sh exactly as they do on a Mac. System32\bash.exe is
# WSL, not Git Bash, so it is looked up next to git instead.
Write-Host '==> installing schedules'
$git = Get-Command git -ErrorAction SilentlyContinue
$bash = @(
    $(if ($git) { Join-Path (Split-Path (Split-Path $git.Source)) 'bin\bash.exe' }),
    (Join-Path $env:ProgramFiles 'Git\bin\bash.exe')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $bash) {
    Write-Warning 'Git Bash not found - schedules skipped. Install Git for Windows and re-run.'
} else {
    $run = Join-Path $Dir 'run.sh'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $jobs = @(
        @{ Name = 'jobradar-weekly'; Cmd = 'run'; Note = 'weekly digest  Mondays 09:00'
           Trigger = (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9:00am) },
        @{ Name = 'jobradar-daily'; Cmd = 'alert'; Note = 'daily alerts   every day 09:30 (watched companies only)'
           Trigger = (New-ScheduledTaskTrigger -Daily -At 9:30am) }
    )
    foreach ($j in $jobs) {
        $action = New-ScheduledTaskAction -Execute $bash -Argument "`"$run`" $($j.Cmd)" -WorkingDirectory $Dir
        Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $j.Trigger `
            -Settings $settings -Description 'Job Radar' -Force | Out-Null
        Write-Host "    $($j.Name)  $($j.Note)"
    }
}

Write-Host @'

Done. Three things left, all yours to do:
  1. Put your resume in this folder and point resume_path at it in config.yaml
  2. Edit run.sh and paste in your secrets (xkiro key + Gmail App Password)
  3. Verify email works, from Git Bash:   ./run.sh test

'@
