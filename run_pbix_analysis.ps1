<#
.SYNOPSIS
    Opens a .pbix file in Power BI Desktop (minimized/background), waits for
    the local Analysis Services engine to come up, runs analyze_pbix.py
    against it, then closes Power BI Desktop.

.PARAMETER PbixPath
    Full path to the .pbix file to analyze.

.PARAMETER ProjectDir
    Path to this repo (contains analyze_pbix.py / pbi_discover.py / pyproject.toml).
    Defaults to the directory this script lives in.

.PARAMETER TimeoutSeconds
    How long to wait for Power BI Desktop to finish loading the model
    before giving up. Default 180 (large models take a while).

.PARAMETER AnalyzeArgs
    Extra args passed straight through to analyze_pbix.py, e.g.
    -AnalyzeArgs '--export','metrics.xlsx','--no-cardinality'

.EXAMPLE
    .\run_pbix_analysis.ps1 -PbixPath "C:\reports\Sales.pbix" -AnalyzeArgs '--export','metrics.xlsx'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PbixPath,
    [string]$ProjectDir = $PSScriptRoot,
    [int]$TimeoutSeconds = 180,
    [string[]]$AnalyzeArgs = @()
)

# NOTE: deliberately NOT setting $ErrorActionPreference = "Stop" globally.
# uv/python write legitimate, non-fatal diagnostics to stderr (analyze_pbix.py
# does this on purpose - see its cardinality/last-refresh warning blocks), and
# with $ErrorActionPreference = "Stop", PowerShell treats every stderr line
# from a native .exe as a terminating error regardless of output redirection.
# That silently truncated the real error in an earlier version of this script
# and would have broken step 4 even on a fully successful run. Instead, each
# external command below is checked explicitly via $LASTEXITCODE.
$ErrorActionPreference = "Continue"

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

# ---- 0. Validate inputs -----------------------------------------------
try {
    $PbixPath = (Resolve-Path -LiteralPath $PbixPath -ErrorAction Stop).Path
}
catch {
    Fail "PBIX file not found: $PbixPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "analyze_pbix.py"))) {
    Fail "analyze_pbix.py not found under ProjectDir: $ProjectDir"
}

# ---- 1. Locate Power BI Desktop ---------------------------------------
$pbiExe = "B:\Programs\Power BI\bin\PBIDesktop.exe"
if (-not (Test-Path -LiteralPath $pbiExe)) {
    Fail "PBIDesktop.exe not found at: $pbiExe"
}

# ---- 2. Launch it minimized (as close to 'silent' as Desktop supports) ----
Write-Host "Opening '$PbixPath' in Power BI Desktop (minimized)..."
$proc = Start-Process -FilePath $pbiExe -ArgumentList "`"$PbixPath`"" `
    -WindowStyle Minimized -PassThru

# ---- 3. Wait until the model has actually finished loading -------------
# Poll pbi_discover.py's own logic (reused, not reimplemented) until it
# reports an instance whose parent PID is the one we just launched (or,
# failing PID match, whose pbix_path matches what we opened) and that
# instance has a resolved port.
#
# Run via `python -c` rather than a script file: for `python -c`, sys.path[0]
# is the current working directory, so as long as we're cd'd into
# $ProjectDir, `import pbi_discover` resolves correctly. A script FILE would
# instead put that file's own directory first on sys.path - if that file
# lives anywhere other than $ProjectDir (e.g. %TEMP%), the import fails with
# ModuleNotFoundError. This was a real bug in an earlier version of this
# script.
$discoverProbe = @'
import sys
from pbi_discover import find_all

target_pid = int(sys.argv[1])
target_path = sys.argv[2].lower()

for inst in find_all():
    parent_ok = False
    try:
        import psutil
        p = psutil.Process(inst.pid)
        parent = p.parent()
        parent_ok = bool(parent and parent.pid == target_pid)
    except Exception:
        pass

    path_ok = bool(inst.pbix_path and inst.pbix_path.lower() == target_path)

    if inst.port and (parent_ok or path_ok):
        print(inst.port)
        sys.exit(0)

sys.exit(1)
'@

Write-Host "Waiting for the local model engine to start (timeout ${TimeoutSeconds}s)..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$port = $null
$lastStderr = $null

Push-Location $ProjectDir
try {
    while ((Get-Date) -lt $deadline) {
        $stderrFile = [System.IO.Path]::GetTempFileName()
        $out = & uv run python -c $discoverProbe $proc.Id $PbixPath 2>$stderrFile
        $exitCode = $LASTEXITCODE
        $lastStderr = Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrFile -ErrorAction SilentlyContinue

        if ($exitCode -eq 0 -and $out) {
            $port = ($out | Select-Object -Last 1).ToString().Trim()
            break
        }
        # exitCode 1 with no stderr just means "not found yet" - keep polling.
        # Anything on stderr is a real error (e.g. a missing dependency) -
        # surface it immediately instead of quietly retrying for the full
        # timeout window.
        if ($lastStderr -and $lastStderr.Trim()) {
            Pop-Location
            try { $proc | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
            Fail "Discovery probe failed:`n$lastStderr"
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    if ((Get-Location).Path -eq (Resolve-Path $ProjectDir).Path) { Pop-Location }
}

if (-not $port) {
    try { $proc | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
    Fail "Timed out waiting for Power BI Desktop to load the model."
}
Write-Host "Model is up on port $port."

# ---- 4. Run the analyzer ------------------------------------------------
Write-Host "Running analyze_pbix.py --port $port ..."
Push-Location $ProjectDir
& uv run python analyze_pbix.py --port $port @AnalyzeArgs
$analyzeExitCode = $LASTEXITCODE
Pop-Location

# ---- 5. Close Power BI Desktop ------------------------------------------
Write-Host "Closing Power BI Desktop (PID $($proc.Id))..."
try {
    if (-not $proc.HasExited) {
        $proc.CloseMainWindow() | Out-Null
        if (-not $proc.WaitForExit(10000)) {
            Write-Host "Graceful close timed out, forcing termination."
            $proc | Stop-Process -Force
        }
    }
}
catch {
    Write-Warning "Could not close Power BI Desktop cleanly: $_"
}

if ($analyzeExitCode -ne 0) {
    Fail "analyze_pbix.py exited with code $analyzeExitCode"
}

Write-Host "Done."