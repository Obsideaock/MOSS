# MOSS one-line installer for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/Obsideaock/MOSS/main/install.ps1 | iex
#
# Testing the experimental branch instead:
#   $env:MOSS_BRANCH="experimental"; irm https://raw.githubusercontent.com/Obsideaock/MOSS/experimental/install.ps1 | iex
#
# What it does, and it's safe to run again (that's also how to repair an install):
#   1. checks for Git and Python 3.10+, and offers to install them with winget if missing
#   2. gets MOSS into %USERPROFILE%\MOSS, or updates the copy already there
#      (an existing Documents\MOSS checkout is used in place, not duplicated)
#   3. makes a Python environment in MOSS\.venv and installs MOSS into it
#   4. adds a `moss` command (MOSS\bin is added to your user PATH)
#   5. runs `moss setup`, which asks whether this is a reader or a Seed Vault
#
# It never deletes your files: edits in an existing checkout are set aside
# with `git stash`, and identities and settings in %USERPROFILE%\.moss are kept.
#
# Options (environment variables): MOSS_DIR, MOSS_BRANCH (main|experimental),
# MOSS_ROLE (reader|vault), MOSS_REPO (a different git URL), MOSS_NO_SETUP=1.

& {
$ErrorActionPreference = "Stop"

function Ok($msg)   { Write-Host "  " -NoNewline; Write-Host ([char]0x2713) -ForegroundColor Green -NoNewline; Write-Host " $msg" }
function Warn($msg) { Write-Host "  ! " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Die($msg)  { Write-Host "  x " -ForegroundColor Red -NoNewline; Write-Host $msg; throw "MOSS install stopped" }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Git-Quiet {
    # git writes progress to stderr; don't let PowerShell treat that as an error
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $out = & git @args 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return @{ Code = $code; Out = ($out | Out-String).Trim() }
}

$RepoUrl = if ($env:MOSS_REPO) { $env:MOSS_REPO } else { "https://github.com/Obsideaock/MOSS.git" }
$Branch  = if ($env:MOSS_BRANCH) { $env:MOSS_BRANCH } else { "main" }
if ($Branch -notin @("main", "experimental")) { Die "MOSS_BRANCH must be main or experimental" }

Write-Host ""
Write-Host "MOSS installer" -ForegroundColor White -NoNewline
Write-Host "  (branch $Branch)"
Write-Host ""

# ---- 1. Git and Python
function Find-Python {
    foreach ($cmd in @(@("py", "-3"), @("python"), @("python3"))) {
        $exe = Get-Command $cmd[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        # The Microsoft Store "python" placeholder lives in WindowsApps and isn't a real Python.
        if ($exe.Source -like "*\WindowsApps\*") { continue }
        $rest = @($cmd | Select-Object -Skip 1)
        try {
            $v = & $cmd[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $v) {
            $parts = "$v".Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { return ,$cmd }
        }
    }
    return $null
}

function Winget-Install($id, $what) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "$what isn't installed, and winget isn't available to install it. Install $what yourself, then run this again."
    }
    $answer = Read-Host "  $what isn't installed. Install it now with winget? [Y/n]"
    if ($answer -and $answer -notmatch "^[Yy]") { Die "MOSS needs $what." }
    winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Winget-Install "Git.Git" "Git" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "Git still isn't found. Open a new PowerShell window and run this again." }
$Py = Find-Python
if (-not $Py) { Winget-Install "Python.Python.3.12" "Python 3"; $Py = Find-Python }
if (-not $Py) { Die "Python 3.10+ still isn't found. Open a new PowerShell window and run this again." }
$PyRest = @($Py | Select-Object -Skip 1)
$pyVersion = & $Py[0] @PyRest --version
Ok "Git and $pyVersion ready"

# ---- 2. the code
function Is-MossCheckout($d) { (Test-Path (Join-Path $d ".git")) -and (Test-Path (Join-Path $d "moss\__init__.py")) }
$Home_ = $env:USERPROFILE
$Docs = [Environment]::GetFolderPath("MyDocuments")
if ($env:MOSS_DIR) { $Dir = $env:MOSS_DIR }
elseif (Is-MossCheckout (Join-Path $Home_ "MOSS")) { $Dir = Join-Path $Home_ "MOSS" }
elseif (Is-MossCheckout (Join-Path $Docs "MOSS")) { $Dir = Join-Path $Docs "MOSS" }
else { $Dir = Join-Path $Home_ "MOSS" }

if ((Test-Path $Dir) -and -not (Is-MossCheckout $Dir)) {
    if (@(Get-ChildItem -Force $Dir).Count -eq 0) { Remove-Item $Dir }
    else { Die "$Dir exists but isn't a MOSS git checkout. Move it aside (or set MOSS_DIR) and run this again." }
}

if (Test-Path $Dir) {
    Write-Host "  updating the MOSS already in $Dir..."
    Set-Location $Dir
    if ((Git-Quiet remote get-url origin).Code -ne 0) { Git-Quiet remote add origin $RepoUrl | Out-Null }
    $dirty = (Git-Quiet status --porcelain --untracked-files=no).Out
    if ($dirty) {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
        Git-Quiet stash push --quiet -m "set aside by the MOSS installer $stamp" | Out-Null
        Warn "your own edits in $Dir are saved with git stash (get them back: cd $Dir; git stash pop)"
    }
    $r = Git-Quiet fetch --quiet origin $Branch
    if ($r.Code -ne 0) { Die "couldn't reach GitHub: $($r.Out)" }
    if ((Git-Quiet rev-parse --verify --quiet $Branch).Code -eq 0) { $r = Git-Quiet checkout --quiet $Branch }
    else { $r = Git-Quiet checkout --quiet -b $Branch --track "origin/$Branch" }
    if ($r.Code -ne 0) { Die "couldn't switch to $($Branch): $($r.Out)" }
    $r = Git-Quiet merge --ff-only --quiet "origin/$Branch"
    if ($r.Code -ne 0) {
        Die "this copy has commits GitHub doesn't, so it can't simply move forward. Push them (or sort it out with git), then run this again. Nothing was changed."
    }
} else {
    Write-Host "  downloading MOSS into $Dir..."
    $r = Git-Quiet clone --quiet --branch $Branch $RepoUrl $Dir
    if ($r.Code -ne 0) { Die "download failed: $($r.Out)" }
    Set-Location $Dir
}
$head = (Git-Quiet log -1 "--format=%h, %cd" --date=short).Out
Ok "MOSS code in $Dir ($head)"

# ---- 3. Python environment
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
$healthy = $false
if (Test-Path $VenvPy) {
    & $VenvPy -c "import sys" 2>$null
    $healthy = ($LASTEXITCODE -eq 0)
}
if (-not $healthy) {
    if (Test-Path (Join-Path $Dir ".venv")) { Remove-Item -Recurse -Force (Join-Path $Dir ".venv") }
    & $Py[0] @PyRest -m venv (Join-Path $Dir ".venv")
    if ($LASTEXITCODE -ne 0) { Die "couldn't make the Python environment" }
}
Write-Host "  installing Python packages..."
# pip prints pages of harmless build notes; show them only on failure.
$PipLog = New-TemporaryFile
& $VenvPy -m pip install --quiet --upgrade pip *> $PipLog
if ($LASTEXITCODE -eq 0) { & $VenvPy -m pip install --quiet -e $Dir *> $PipLog }
if ($LASTEXITCODE -ne 0) {
    Get-Content $PipLog -Tail 30
    Remove-Item $PipLog -ErrorAction SilentlyContinue
    Die "installing the Python packages failed (see above)"
}
Remove-Item $PipLog -ErrorAction SilentlyContinue
Ok "Python packages installed"

# ---- 4. the moss command
$Bin = Join-Path $Dir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Set-Content -Path (Join-Path $Bin "moss.cmd") -Encoding ASCII -Value "@`"%~dp0..\.venv\Scripts\moss.exe`" %*"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath.Split(";") | Where-Object { $_ -eq $Bin }).Count -eq 0) {
    $newPath = if ($userPath) { "$userPath;$Bin" } else { $Bin }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Ok "the moss command is ready (new terminal windows will have it)"
} else {
    Ok "the moss command is ready"
}
if (($env:Path.Split(";") | Where-Object { $_ -eq $Bin }).Count -eq 0) { $env:Path = "$env:Path;$Bin" }

# ---- 5. setup
if ($env:MOSS_NO_SETUP -eq "1") { Ok "done. Run  moss setup  when you're ready."; return }
$setupArgs = @("setup", "--branch", $Branch)
if ($env:MOSS_ROLE) { $setupArgs += @("--role", $env:MOSS_ROLE) }
Write-Host ""
& (Join-Path $Dir ".venv\Scripts\moss.exe") @setupArgs
}
