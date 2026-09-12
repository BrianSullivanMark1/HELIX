# make_shortcut.ps1 - put HELIX on the Desktop and the Start Menu.
#
# ASCII ONLY in this file and in everything it prints (HELIX_MARK1_PLAN.md rule 7): a non-ASCII
# character in a .ps1 or on a Windows console is how these scripts break on someone else's machine.
#
# Usage, from anywhere:
#     powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
#
#     -Python <path>   use this interpreter instead of auto-detecting
#     -Frozen          point at dist\HELIX\HELIX.exe (run python build.py first) instead of source
#     -NoDesktop       skip the Desktop shortcuts
#     -NoStartMenu     skip the Start Menu shortcuts
#     -Install         install what HELIX needs first: pip requirements, and the React face
#                      (npm install + npm run build in web/) if it has never been built
#     -Venv            keep HELIX's packages in .venv inside the repo instead of your system
#                      Python. Creates it if missing and implies -Install. RECOMMENDED: HELIX
#                      pulls numpy 2.x, which breaks matplotlib/contourpy pins in a shared
#                      global interpreter and takes your other projects down with it.
#     -Remove          delete the shortcuts this script creates, and stop
#
# THE DESKTOP GETS ONE ICON: "HELIX". That is the one you click.
# The Start Menu additionally gets "HELIX (console)" - the same launch with the console window
# left open, so when something fails at startup the reason is on screen instead of nowhere.
# It lives in the Start Menu rather than the Desktop because you want it about twice a year and
# a second icon on the Desktop is clutter the rest of the time. Search "HELIX console" to find it.
#
# Both run from the repo, so they pick up code changes with no rebuild.

[CmdletBinding()]
param(
    [string] $Python = "",
    [switch] $Frozen,
    [switch] $NoDesktop,
    [switch] $NoStartMenu,
    [switch] $Install,
    [switch] $Venv,
    [switch] $Remove
)

$ErrorActionPreference = "Stop"

function Write-Step  ([string] $m) { Write-Host "  $m" }
function Write-Ok    ([string] $m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Write-Warn2 ([string] $m) { Write-Host "  WARN $m" -ForegroundColor Yellow }
function Write-Fail  ([string] $m) { Write-Host "  FAIL $m" -ForegroundColor Red }

# ---------------------------------------------------------------- paths

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root      = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$MainPy    = Join-Path $Root "main.py"
$WebDist   = Join-Path $Root "web\dist\index.html"
$IconPath  = Join-Path $Root "assets\orb.ico"
if (-not (Test-Path $IconPath)) { $IconPath = Join-Path $Root "assets\helix.ico" }

$DesktopDir   = [Environment]::GetFolderPath("Desktop")
$StartMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "HELIX"

$Names = @("HELIX", "HELIX (console)")

Write-Host ""
Write-Host "HELIX shortcut installer"
Write-Host "------------------------"
Write-Step "repo: $Root"

# ---------------------------------------------------------------- remove

if ($Remove) {
    $removed = 0
    foreach ($n in $Names) {
        $d = Join-Path $DesktopDir "$n.lnk"
        if (Test-Path $d) { Remove-Item $d -Force; Write-Ok "removed $d"; $removed++ }
    }
    if (Test-Path $StartMenuDir) {
        Remove-Item $StartMenuDir -Recurse -Force
        Write-Ok "removed $StartMenuDir"
        $removed++
    }
    if ($removed -eq 0) { Write-Step "nothing to remove." }
    Write-Host ""
    exit 0
}

if (-not (Test-Path $MainPy)) {
    Write-Fail "main.py not found at $MainPy"
    Write-Step "Run this from the HELIX repo: scripts\make_shortcut.ps1"
    exit 1
}
if (Test-Path $IconPath) { Write-Step "icon: $IconPath" }
else { Write-Warn2 "no icon found in assets\ - the shortcut will use the default one." }

# ---------------------------------------------------------------- interpreter

function Resolve-Interpreter {
    param([string] $Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        Write-Fail "-Python was given but does not exist: $Explicit"
        exit 1
    }
    # A venv inside the repo wins: it is the interpreter that has HELIX's requirements installed.
    foreach ($rel in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe", ".env\Scripts\python.exe")) {
        $p = Join-Path $Root $rel
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $found = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { return $found.Trim() }
    }
    return ""
}

$Target      = ""
$TargetW     = ""
$MakeConsole = $false

if ($Frozen) {
    $exe = Join-Path $Root "dist\HELIX\HELIX.exe"
    if (-not (Test-Path $exe)) {
        Write-Fail "dist\HELIX\HELIX.exe not found."
        Write-Step "Build it first:  python build.py --with-voice"
        Write-Step "Or drop -Frozen to make source-mode shortcuts that need no build."
        exit 1
    }
    $Target = $exe
    Write-Step "mode: frozen build"
    Write-Step "exe:  $exe"
}
else {
    # A repo-local venv, when asked for. Resolve-Interpreter already PREFERS .venv over system
    # Python, so creating it here is all that is needed - everything downstream then points at it
    # on its own, including future runs of this script with no flags at all.
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if ($Venv -and -not (Test-Path $venvPy)) {
        $base = Resolve-Interpreter -Explicit $Python
        if (-not $base) {
            Write-Fail "No Python interpreter found to build a venv from."
            exit 1
        }
        Write-Step "creating .venv (from $base)"
        $prevEAPv = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $base -m venv (Join-Path $Root ".venv")
        $venvRc = $LASTEXITCODE
        $ErrorActionPreference = $prevEAPv
        if ($venvRc -ne 0 -or -not (Test-Path $venvPy)) {
            Write-Fail "venv creation failed (exit $venvRc). Falling back to $base."
        } else {
            Write-Ok "created .venv"
            $Install = $true   # a brand-new venv has nothing in it
        }
    }

    $py = Resolve-Interpreter -Explicit $Python
    if (-not $py) {
        Write-Fail "No Python interpreter found."
        Write-Step "Pass one explicitly:  scripts\make_shortcut.ps1 -Python C:\path\to\python.exe"
        exit 1
    }
    $pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    if (-not (Test-Path $pyw)) { $pyw = $py }   # no windowed twin - fall back, console and all

    $Target      = $pyw     # HELIX            -> no console
    $TargetW     = $py      # HELIX (console)  -> console stays open
    $MakeConsole = $true

    Write-Step "mode: source (no build needed; picks up code changes)"
    if ($py -like "$Root*") { Write-Step "python:  $py  (repo venv - isolated from your other projects)" }
    else { Write-Step "python:  $py  (SYSTEM python - shared with your other projects; -Venv isolates it)" }
    if ($pyw -ne $py) { Write-Step "pythonw: $pyw" }

    # A missing dependency is the most common reason a fresh shortcut opens and closes again.
    # Say so here rather than letting the user discover it as a window that blinks.
    #
    # Native commands need EAP=Continue around them. With $ErrorActionPreference = "Stop",
    # ANY line a native process writes to stderr becomes a TERMINATING error - so a probe
    # that is supposed to report a missing package instead kills this script before it
    # creates a single shortcut. Merge stderr into the success stream, discard it, and read
    # the exit code, which is the only thing we actually asked for.
    $depsOk = $false
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = & $py -c "import fastapi, uvicorn" 2>&1
        $depsOk = ($LASTEXITCODE -eq 0)
    } catch {
        $depsOk = $false
    } finally {
        $ErrorActionPreference = $prevEAP
    }

    $reqs = Join-Path $Root "requirements.txt"

    if ($depsOk) {
        Write-Ok "interpreter has HELIX's web dependencies"
    }
    elseif ($Venv -and -not $Install) {
        Write-Warn2 "A .venv exists but is missing dependencies. Re-run with -Install."
    }
    elseif ($Install) {
        Write-Warn2 "Dependencies missing - installing them now. This takes a few minutes."
        Write-Step "  $py -m pip install -r $reqs"
        $ErrorActionPreference = "Continue"
        & $py -m pip install -r $reqs
        $pipRc = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        if ($pipRc -eq 0) {
            Write-Ok "dependencies installed"
        } else {
            Write-Fail "pip failed (exit $pipRc). The shortcuts below are still created;"
            Write-Fail "fix the install, then double-click HELIX again."
        }
    }
    else {
        Write-Warn2 "That interpreter cannot import fastapi/uvicorn yet - HELIX will not start."
        Write-Warn2 "Either re-run this with -Install, or run this once yourself:"
        Write-Warn2 "  `"$py`" -m pip install -r `"$reqs`""
        Write-Warn2 "Making the shortcuts anyway so they are ready when the install finishes."
    }

    # THE WEB FACE. HELIX's UI is a React app; the backend serves web/dist. Without it the app
    # starts, answers, and shows "the web face isn't built yet" instead of the orb - which looks
    # like a broken install but is only a missing build step. Check it here so the shortcut is
    # never handed over in that state without saying so.
    $webDir = Join-Path $Root "web"
    if (Test-Path $WebDist) {
        Write-Ok "web face is built"
    }
    elseif ($Install) {
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
        if (-not $npm) {
            Write-Fail "npm not found - install Node.js, then re-run with -Install."
            Write-Fail "Without it HELIX runs backend-only and shows a placeholder instead of the orb."
        }
        else {
            Write-Warn2 "Web face not built - building it now. First run takes a few minutes."
            $ErrorActionPreference = "Continue"
            Push-Location $webDir
            try {
                & $npm.Source install
                $npmRc = $LASTEXITCODE
                if ($npmRc -eq 0) { & $npm.Source run build; $npmRc = $LASTEXITCODE }
            } finally {
                Pop-Location
                $ErrorActionPreference = $prevEAP
            }
            if ($npmRc -eq 0 -and (Test-Path $WebDist)) { Write-Ok "web face built" }
            else { Write-Fail "web build failed (exit $npmRc) - HELIX will run backend-only." }
        }
    }
    else {
        Write-Warn2 "Web face not built - HELIX will start but show a placeholder, not the orb."
        Write-Warn2 "Re-run this with -Install, or run these two once yourself:"
        Write-Warn2 "  cd /d `"$webDir`"  &&  npm install  &&  npm run build"
    }
}

# ---------------------------------------------------------------- write the shortcuts

$Shell = New-Object -ComObject WScript.Shell

function New-HelixShortcut {
    param(
        [string] $LinkPath,
        [string] $Exe,
        [string] $Arguments,
        [string] $Description,
        [int]    $WindowStyle = 1     # 1 = normal, 7 = minimized
    )
    $sc = $Shell.CreateShortcut($LinkPath)
    $sc.TargetPath       = $Exe
    if ($Arguments) { $sc.Arguments = $Arguments }
    $sc.WorkingDirectory = $Root
    $sc.Description      = $Description
    $sc.WindowStyle      = $WindowStyle
    if (Test-Path $IconPath) { $sc.IconLocation = "$IconPath,0" }
    $sc.Save()
    Write-Ok $LinkPath
}

$targets = @()
if ($Frozen) {
    $targets += @{ Name = "HELIX"; Exe = $Target; Args = ""; Desc = "HELIX (frozen build)"; Style = 1 }
}
else {
    $q = '"' + $MainPy + '"'
    $targets += @{ Name = "HELIX"; Exe = $Target; Args = $q; Desc = "HELIX - the orb, from source"; Style = 1 }
    if ($MakeConsole) {
        $targets += @{ Name = "HELIX (console)"; Exe = $TargetW; Args = $q
                       Desc = "HELIX with its console visible - use this when something fails to start"
                       Style = 1 }
    }
}

Write-Host ""
if (-not $NoDesktop) {
    # ONE icon on the Desktop, deliberately: the one you actually click. The console variant is a
    # debugging tool, and a debugging tool that sits on the Desktop every day is just clutter -
    # it goes in the Start Menu below, where it costs nothing and is one search away.
    Write-Step "Desktop:"
    $primary = $targets[0]
    New-HelixShortcut -LinkPath (Join-Path $DesktopDir ($primary.Name + ".lnk")) `
                      -Exe $primary.Exe -Arguments $primary.Args `
                      -Description $primary.Desc -WindowStyle $primary.Style
    # An older run of this script put the console shortcut on the Desktop too. Clean it up so
    # re-running is how you FIX the duplicate rather than another way to get one.
    $stale = Join-Path $DesktopDir "HELIX (console).lnk"
    if (Test-Path $stale) { Remove-Item $stale -Force; Write-Step "removed the old duplicate: $stale" }
}
if (-not $NoStartMenu) {
    Write-Step "Start Menu:"
    if (-not (Test-Path $StartMenuDir)) { New-Item -ItemType Directory -Path $StartMenuDir | Out-Null }
    foreach ($t in $targets) {
        New-HelixShortcut -LinkPath (Join-Path $StartMenuDir ($t.Name + ".lnk")) `
                          -Exe $t.Exe -Arguments $t.Args -Description $t.Desc -WindowStyle $t.Style
    }
}

Write-Host ""
Write-Host "Done. Double-click HELIX on the Desktop - that is the only icon you need."
if ($MakeConsole) {
    Write-Host "If it misbehaves, open 'HELIX (console)' from the Start Menu: same launch, but the"
    Write-Host "window stays up with the reason on it."
}
Write-Host "HELIX guards against a second instance, so an extra double-click is harmless."
if (-not $Frozen -and -not $depsOk -and -not $Install) {
    Write-Host ""
    Write-Host "NOTE: install the dependencies before the icon will do anything:" -ForegroundColor Yellow
    Write-Host "      double-click install-shortcut.bat again with -Install, or run the pip line above."
}
Write-Host "To undo:  install-shortcut.bat -Remove"
Write-Host ""
