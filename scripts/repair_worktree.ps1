# Repair HELIX_V3's git when the worktree registration under C:\Users\brian\HELIX\.git is gone.
#
# Symptom:  git status  ->  fatal: not a git repository: C:/Users/brian/HELIX/.git/worktrees/HELIX_V2
# Cause:    HELIX_V3 is a git WORKTREE of C:\Users\brian\HELIX (its .git is a one-line pointer file).
#           The backbone repo's .git\worktrees\HELIX_V2 folder holds this worktree's HEAD/index; when
#           that folder is lost (a recycle/restore of the HELIX folder, a prune), V3's git dies even
#           though every commit is safe in the backbone.
# Fix:      recreate the three tiny registration files, then rebuild the index from the branch tip
#           (a mixed reset — the working tree is NOT touched; uncommitted edits stay as modifications).
#
# Run from any PowerShell:   powershell -ExecutionPolicy Bypass -File C:\Users\brian\HELIX_V3\scripts\repair_worktree.ps1

$backbone = "C:\Users\brian\HELIX\.git"
$worktree = "C:\Users\brian\HELIX_V3"
$reg = Join-Path $backbone "worktrees\HELIX_V2"
$branch = "v3"

if (-not (Test-Path $backbone)) { Write-Error "Backbone repo missing at $backbone - restore C:\Users\brian\HELIX from the Recycle Bin first."; exit 1 }
if (-not (Test-Path $reg)) { New-Item -ItemType Directory -Force $reg | Out-Null }

# Plain ASCII, LF, no BOM - git is picky about these files.
[IO.File]::WriteAllText((Join-Path $reg "HEAD"), "ref: refs/heads/$branch`n")
[IO.File]::WriteAllText((Join-Path $reg "commondir"), "../..`n")
[IO.File]::WriteAllText((Join-Path $reg "gitdir"), ($worktree.Replace("\", "/") + "/.git`n"))
# The worktree's own pointer file is hidden + read-only (git's doing) and is normally already
# correct — only rewrite it when its content is wrong, clearing the attributes for the write.
$pointer = Join-Path $worktree ".git"
$want = "gitdir: " + ($backbone.Replace("\", "/")) + "/worktrees/HELIX_V2`n"
$have = if (Test-Path $pointer) { [IO.File]::ReadAllText($pointer) } else { "" }
if ($have.Trim() -ne $want.Trim()) {
    if (Test-Path $pointer) { Set-ItemProperty -Path $pointer -Name Attributes -Value 'Normal' }
    [IO.File]::WriteAllText($pointer, $want)
    Set-ItemProperty -Path $pointer -Name Attributes -Value 'Hidden'
}

Set-Location $worktree
git reset -q            # rebuild the (lost) index from HEAD; working files untouched
git status --short | Select-Object -First 15
Write-Host ""
Write-Host "Branch: $(git branch --show-current)   HEAD: $(git log --oneline -1)"
Write-Host "If the list above shows your edits as ' M' lines, git is back. Commit as usual."
