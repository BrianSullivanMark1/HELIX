# HELIX auto-start (opt-in, §39): create a Startup-folder shortcut that launches the always-on
# supervisor at login, so HELIX comes back by itself after a reboot. This is NOT Windows Task
# Scheduler — it just auto-opens the app, which is Brian's chosen always-on model (the QTimer cadence
# does the scheduling once the app is up). Run once from the repo root:
#
#     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#
# To undo: press Win+R, type  shell:startup , Enter, and delete HELIX.lnk.

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot                 # repo root (parent of scripts\)
$supervisor = Join-Path $repo "scripts\run_helix.py"

# Prefer pythonw.exe (no console window) for a clean background launch; fall back to python.exe.
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python not found on PATH. Install Python (or add it to PATH), then re-run." }

$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "HELIX.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $py
$shortcut.Arguments = "`"$supervisor`""
$shortcut.WorkingDirectory = $repo
$shortcut.WindowStyle = 7                                # minimized
$shortcut.Description = "HELIX always-on (auto-restart supervisor)"
$shortcut.Save()

Write-Host "Installed HELIX auto-start shortcut:"
Write-Host "  $lnk"
Write-Host "  -> $py `"$supervisor`""
Write-Host ""
Write-Host "HELIX will now launch at login via the always-on supervisor."
Write-Host "To undo: open 'shell:startup' (Win+R) and delete HELIX.lnk."
