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
# Strip a literal leading/trailing " if the caller passed the path already
# quoted (e.g. -PbixPath '"C:\...\Pizza sales.pbix"') - PowerShell only
# treats quotes as delimiters when they're part of its own tokenizing, so a
# quote character pasted inside the string value itself passes straight
# through and would otherwise make Resolve-Path fail to find the file.
$PbixPath = $PbixPath.Trim('"')

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
# failing PID match, whose pbix_path matches what we opened), AND that
# instance's engine actually has a queryable catalog/database attached -
# not merely that msmdsrv.exe has bound a port.
#
# That distinction matters: msmdsrv.exe writes msmdsrv.port.txt (and
# accepts connections) before it has necessarily finished attaching the
# model as a catalog. A real, observed failure: analyze_pbix.py connected
# successfully on the port the moment it appeared, then immediately hit
# "No catalogs/databases found on this instance." on its very first DMV
# query, because the catalog wasn't registered yet. So readiness here is
# verified by actually opening a PbiConnection (the same class the real
# analyzer uses) and confirming it can resolve a catalog - not just that
# the port is open.
#
# Written to a real file rather than passed via `python -c`: PowerShell's
# native-command argument encoding mangles embedded double quotes in a
# multi-line string passed as a single argument (observed: `f"{inst.port}`
# got silently corrupted to `f{inst.port}`, a SyntaxError) - a script FILE
# sidesteps that entirely since the code never travels through the command
# line. To still make `import pbi_discover` work regardless of where this
# file is written, $ProjectDir is added to sys.path explicitly (argv[3])
# instead of relying on cwd or the script's own folder.
#
# Also prints the REAL PBIDesktop.exe PID (the msmdsrv.exe instance's own
# parent process), not just the port. Some Power BI Desktop installs run a
# small launcher stub that exits right after spawning the actual editor
# process, so the PID Start-Process hands back to PowerShell isn't
# necessarily the one that's still open at the end - closing/killing by
# that PID alone can silently do nothing while the real window stays open.
#
# Exit codes (deliberately distinct so PowerShell can tell "keep waiting"
# apart from "something is actually broken"):
#   0  - ready: port printed, safe to proceed
#   10 - no matching msmdsrv instance found yet (keep polling silently)
#   20 - matching instance found, but its catalog isn't queryable yet
#        (keep polling; NOTREADY reason on stderr is informational only)
#   30 - a genuine, unexpected error (e.g. missing ADOMD DLL) - stop and
#        report it rather than retrying uselessly for the full timeout
$discoverProbe = @'
import sys
sys.path.insert(0, sys.argv[3])

target_pid = int(sys.argv[1])
target_path = sys.argv[2].lower()

try:
    from pbi_discover import find_all

    for inst in find_all():
        real_pid = None
        parent_ok = False
        try:
            import psutil
            p = psutil.Process(inst.pid)
            parent = p.parent()
            if parent:
                real_pid = parent.pid
                parent_ok = (parent.pid == target_pid)
        except Exception:
            pass

        path_ok = bool(inst.pbix_path and inst.pbix_path.lower() == target_path)

        if not (inst.port and (parent_ok or path_ok)):
            continue

        try:
            from pbi_connection import PbiConnection
            conn = PbiConnection(port=inst.port)
            conn.close()
        except Exception as exc:
            sys.stderr.write("NOTREADY " + type(exc).__name__ + ": " + str(exc) + "\n")
            sys.exit(20)

        result_pid = real_pid if real_pid else target_pid
        print(str(inst.port) + " " + str(result_pid))
        sys.exit(0)

    sys.exit(10)
except SystemExit:
    raise
except Exception as exc:
    sys.stderr.write("ERROR " + type(exc).__name__ + ": " + str(exc) + "\n")
    sys.exit(30)
'@
$probeFile = Join-Path $env:TEMP ("pbi_discover_probe_" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -LiteralPath $probeFile -Value $discoverProbe -Encoding UTF8

Write-Host "Waiting for the local model engine to start (timeout ${TimeoutSeconds}s)..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$port = $null
$editorPid = $proc.Id  # fallback if the probe can't resolve the real editor PID
$lastStderr = $null

Push-Location $ProjectDir
try {
    while ((Get-Date) -lt $deadline) {
        $stderrFile = [System.IO.Path]::GetTempFileName()
        $out = & uv run python $probeFile $proc.Id $PbixPath $ProjectDir 2>$stderrFile
        $exitCode = $LASTEXITCODE
        $lastStderr = Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrFile -ErrorAction SilentlyContinue

        if ($exitCode -eq 0 -and $out) {
            $parts = ($out | Select-Object -Last 1).ToString().Trim() -split '\s+'
            $port = $parts[0]
            if ($parts.Length -gt 1 -and $parts[1] -match '^\d+$') {
                $editorPid = [int]$parts[1]
            }
            break
        }
        elseif ($exitCode -eq 10) {
            # No matching msmdsrv instance yet - completely normal early on.
        }
        elseif ($exitCode -eq 20) {
            # Engine is up but the catalog isn't attached yet - normal, keep
            # waiting. Surface the reason once in a while for visibility.
            if ($lastStderr) { Write-Host "  ($($lastStderr.Trim()))" }
        }
        else {
            Pop-Location
            Remove-Item -LiteralPath $probeFile -ErrorAction SilentlyContinue
            try { $proc | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
            Fail "Discovery probe failed (exit $exitCode):`n$lastStderr"
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    Remove-Item -LiteralPath $probeFile -ErrorAction SilentlyContinue
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

# ---- 5. Close Power BI Desktop (forcefully if it won't close nicely) ----
# Closes BOTH the PID Start-Process originally returned AND the real editor
# PID resolved during discovery (see the note above the probe) - on some
# installs these differ, and closing only one can leave the other's window
# open. For each: try a graceful CloseMainWindow first, then Stop-Process
# -Force, then (if it's still alive - e.g. child processes not covered by
# a single Stop-Process) `taskkill /F /T` as a final, no-excuses fallback.
$pidsToClose = @($proc.Id, $editorPid) | Select-Object -Unique
Write-Host "Closing Power BI Desktop (PID(s): $($pidsToClose -join ', '))..."

foreach ($targetPid in $pidsToClose) {
    $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if (-not $p) { continue }

    try {
        $p.CloseMainWindow() | Out-Null
        $p.WaitForExit(8000) | Out-Null
    }
    catch {}

    $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($p) {
        Write-Host "  PID $targetPid still running - forcing termination."
        try { Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Milliseconds 500
    }

    $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($p) {
        Write-Host "  PID $targetPid still present - using taskkill /F /T as last resort."
        & taskkill /PID $targetPid /F /T 2>$null | Out-Null
    }
}

Start-Sleep -Milliseconds 500
$stillRunning = $pidsToClose | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } | Where-Object { $_ }
if ($stillRunning) {
    Write-Warning "Power BI Desktop process(es) still running after force-close attempts: $($stillRunning.Id -join ', ')"
}
else {
    Write-Host "Power BI Desktop closed."
}

if ($analyzeExitCode -ne 0) {
    Fail "analyze_pbix.py exited with code $analyzeExitCode"
}

Write-Host "Done."