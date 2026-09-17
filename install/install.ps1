<#
.SYNOPSIS
    Hippocampus v0.2 First User Release — one-command Windows installer.

.DESCRIPTION
    Thin PowerShell wrapper. The real work is done by the Python module
    `v3core.first_run.run_install` (invoked via the `hippocampus install`
    console subcommand). This script:

      1. Checks for a usable PowerShell (5.1+).
      2. Checks for Python 3.10 / 3.11 / 3.12 on PATH.
      3. Checks for `uv` on PATH (installs it when missing).
      4. Checks for Docker (warns early; the Python module will FAIL
         loudly with an actionable message if Docker is missing).
      5. Locates the `hippocampus` console script; prefers the one inside
         the active Python environment.
      6. Delegates to `hippocampus install` with the parameters passed in.

    Designed to be run as:

        irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 | iex

    Idempotent: re-running never duplicates containers, never overwrites
    an existing config without an explicit flag, never double-edits the
    Hermes config.

.PARAMETER Preset
    Which preset to install.  Valid values: siliconflow, custom.
    Default: siliconflow.

.PARAMETER PgPort
    The local port to run the disposable pgvector container on.
    Default: 55432 (port 5433 is unconditionally refused).

.PARAMETER ProfileDir
    Override the profile directory.  Default: ~/.v3-core/profiles/default.

.PARAMETER EmbedKey
    Embedding API key.  Never printed in any output (only redacted).

.PARAMETER LlmKey
    Memory LLM API key.  Never printed in any output (only redacted).

.PARAMETER LlmBaseUrl
    Override the LLM base URL.

.PARAMETER LlmModel
    Override the LLM model name.

.PARAMETER SkipSmoke
    Skip the write + readback + recall smoke test.

.PARAMETER Help
    Print usage and exit.

.EXAMPLE
    iex (irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1)
    # Uses all defaults — siliconflow preset, port 55432, no API keys.

.EXAMPLE
    iex (irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1) -LlmKey 'sk-...'
    # Installs with an explicit LLM key.

.NOTES
    This script is intentionally small — every byte of real logic lives in
    src/v3-core/src/v3core/first_run.py.  If you change behavior, change
    the Python module, not this script.
#>

[CmdletBinding()]
param(
    [ValidateSet("siliconflow", "custom")]
    [string]$Preset = "siliconflow",
    [ValidateRange(1024, 65535)]
    [int]$PgPort = 55432,
    [string]$ProfileDir = "",
[string]$PluginWheel = "",
    [string]$EmbedKey = "",
    [string]$LlmKey = "",
    [string]$LlmBaseUrl = "",
    [string]$LlmModel = "",
    [switch]$SkipSmoke,
    [switch]$Help
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$ProgressPreference = "Continue"
$RepoRawBase = "https://raw.githubusercontent.com/yuzi001a/hippocampus-memory"

# ---------------------------------------------------------------------------
# Banner + help
# ---------------------------------------------------------------------------

function Write-Banner {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host "  Hippocampus v0.2 First User Release - installer" -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Show-Help {
    @"
Hippocampus installer (Windows PowerShell).

USAGE
    irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 | iex
    iex (irm ...install.ps1) -Preset custom -PgPort 55432 -LlmKey 'sk-...'

OPTIONS
    -Preset       siliconflow | custom       (default: siliconflow)
    -PgPort       local port for disposable pg (default: 55432; 5433 is refused)
    -ProfileDir   override profile directory   (default: ~/.v3-core/profiles/default)
    -EmbedKey     embedding API key           (optional; SKIP if absent)
    -LlmKey       memory LLM API key          (optional; SKIP if absent)
    -LlmBaseUrl   override LLM base URL       (optional)
    -LlmModel     override LLM model name     (optional)
    -SkipSmoke    skip the write+recall smoke (optional)

WHAT IT DOES
    1. Checks Python / uv / Docker.
    2. Delegates to `hippocampus install` (a thin Python entry point).
    3. Prints a verdict block at the end with PASS/FAIL/SKIP per step.

SECRETS
    API keys and passwords are NEVER printed. They are redacted to
    first-4-chars + '...' in any output.

IDEMPOTENT
    Re-running never duplicates containers, never overwrites an existing
    config without --overwrite-config, never double-edits the Hermes config.
"@
}

if ($Help) {
    Show-Help
    exit 0
}

Write-Banner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Test-MinPython {
    param([string]$Cmd)

    if (-not (Get-Command $Cmd -ErrorAction SilentlyContinue)) {
        return $null
    }
    try {
        $v = & $Cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        return [version]$v
    } catch {
        return $null
    }
}

function Resolve-Python {
    # Prefer 'python' on PATH; fall back to 'py -3' on Windows.
    $candidates = @("python", "py", "python3")
    foreach ($c in $candidates) {
        $v = Test-MinPython -Cmd $c
        if ($v -and $v.Major -eq 3 -and $v.Minor -ge 10 -and $v.Minor -le 12) {
            return @{ Cmd = $c; Ver = $v }
        }
    }
    # No acceptable python found.
    return $null
}

function Resolve-Uv {
    $uv = Get-Command "uv" -ErrorAction SilentlyContinue
    if ($uv) {
        return $uv.Source
    }
    return $null
}

function Install-Uv {
    Write-Host "[installer] uv not found; installing via the official installer..."
    try {
        irm https://astral.sh/uv/install.ps1 | iex
    } catch {
        Write-Host ""
        Write-Host "FAIL: could not install uv automatically: $_" -ForegroundColor Red
        Write-Host "      Install uv manually (https://docs.astral.sh/uv/) and re-run." -ForegroundColor Red
        return $null
    }
    return Resolve-Uv
}

function Resolve-Docker {
    $dk = Get-Command "docker" -ErrorAction SilentlyContinue
    if (-not $dk) {
        return @{ Present = $false; Running = $false }
    }
    try {
        & docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
        $running = ($LASTEXITCODE -eq 0)
    } catch {
        $running = $false
    }
    return @{ Present = $true; Running = $running }
}

function Resolve-Hippocampus {
    param([string]$PythonCmd)

    $hippo = Get-Command "hippocampus" -ErrorAction SilentlyContinue
    if ($hippo) { return $hippo.Source }

    # Fallback: try to import the module via the chosen python and dispatch
    # through `python -m v3core.distribution_cli install`.  The main agent's
    # CLI code wires the `install` subcommand.
    if ($PythonCmd) {
        try {
            & $PythonCmd -c "import v3core.distribution_cli" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return "$PythonCmd -m v3core.distribution_cli"
            }
        } catch { }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Managed install layout (the "no venv, no build, no clone" path)
# ---------------------------------------------------------------------------

$ManagedRoot   = Join-Path $env:USERPROFILE ".hippocampus"
$ManagedVenv   = Join-Path $ManagedRoot "venv"
$ManagedPython = Join-Path $ManagedVenv "Scripts\python.exe"
$ManagedBin    = Join-Path $ManagedRoot "bin"
$ManagedShim   = Join-Path $ManagedBin "hippocampus.cmd"
$PluginWheel   = $null

function Install-Hippocampus {
    <#
      Installs the two release packages into a managed environment and returns a
      runnable `hippocampus` command.

      Order of preference:
        1. GitHub release assets (wheels attached to the latest release)
        2. the repository source tarball (works before any release exists)
      Never needs git, a compiler, or a pre-existing venv.
    #>
    param([string]$UvPath)

    $repo    = "yuzi001a/hippocampus-memory"
    $relBase = "https://github.com/$repo/releases/latest/download"
    $srcUrl  = "https://codeload.github.com/$repo/tar.gz/refs/heads/main"
    $tmp     = Join-Path ([System.IO.Path]::GetTempPath()) ("hippocampus-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null

    $coreTarget = $null
    $plugTarget = $null

    # 1) release assets — discovered by NAME PATTERN, never by a pinned version.
    # Pinning "v3_core-4.0.0-...whl" meant the next version bump silently 404'd
    # both wheels, and the fallback below then served whatever the default branch
    # happened to be: a first user would install old code from a command that
    # looked like it worked. Ask the release API what is actually attached.
    $coreAsset = $null
    $plugAsset = $null
    try {
        $apiHeaders = @{ "User-Agent" = "hippocampus-installer" }
        $rel = Invoke-RestMethod -UseBasicParsing -Headers $apiHeaders `
            -Uri "https://api.github.com/repos/$repo/releases/latest" -ErrorAction Stop
        foreach ($asset in @($rel.assets)) {
            if ($asset.name -like "v3_core-*.whl") { $coreAsset = $asset }
            if ($asset.name -like "v3_hermes_plugin-*.whl") { $plugAsset = $asset }
        }
        foreach ($pair in @(
            @{ Asset = $coreAsset; Slot = "core" },
            @{ Asset = $plugAsset; Slot = "plug" })) {
            if (-not $pair.Asset) { continue }
            $dest = Join-Path $tmp $pair.Asset.name
            Invoke-WebRequest -UseBasicParsing -Headers $apiHeaders `
                -Uri $pair.Asset.browser_download_url -OutFile $dest -ErrorAction Stop
            if ($pair.Slot -eq "core") { $coreTarget = $dest } else { $plugTarget = $dest }
        }
    } catch {
        Write-Host "[installer] release lookup failed ($($_.Exception.Message)) — trying the source tarball." -ForegroundColor Yellow
    }

    # 2) source tarball fallback
    if (-not $coreTarget -or -not $plugTarget) {
        Write-Host "[installer] release assets unavailable — using the source tarball." -ForegroundColor Yellow
        $tarball = Join-Path $tmp "source.tar.gz"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $srcUrl -OutFile $tarball -ErrorAction Stop
        } catch {
            return @{ Ok = $false; Reason = "could not download $srcUrl : $_" }
        }
        try {
            & tar -xzf $tarball -C $tmp 2>$null
        } catch {
            return @{ Ok = $false; Reason = "could not extract the source tarball (tar is missing?)" }
        }
        $tree = Get-ChildItem -Path $tmp -Directory | Where-Object { $_.Name -like "hippocampus-memory-*" } | Select-Object -First 1
        if (-not $tree) { return @{ Ok = $false; Reason = "source tarball did not contain the expected tree" } }
        $coreTarget = Join-Path $tree.FullName "src\v3-core"
        $plugTarget = Join-Path $tree.FullName "src\v3-hermes-plugin"
    }

    # 3) managed environment
    if (-not (Test-Path $ManagedPython)) {
        & $UvPath venv --python 3.11 $ManagedVenv 2>&1 | Out-Null
    }
    if (-not (Test-Path $ManagedPython)) {
        return @{ Ok = $false; Reason = "uv could not create a Python 3.11 environment at $ManagedVenv" }
    }
    & $UvPath pip install --python $ManagedPython $coreTarget $plugTarget 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) {
        return @{ Ok = $false; Reason = "uv pip install failed (exit $LASTEXITCODE)" }
    }

    # 4) a stable `hippocampus` command on PATH
    New-Item -ItemType Directory -Force -Path $ManagedBin | Out-Null
    $shim = "@echo off`r`n`"$ManagedPython`" -m v3core.distribution_cli %*`r`n"
    Set-Content -Path $ManagedShim -Value $shim -Encoding ASCII
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$ManagedBin*") {
        [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";" + $ManagedBin), "User")
    }
    if ($env:Path -notlike "*$ManagedBin*") { $env:Path = "$ManagedBin;$env:Path" }

    return @{ Ok = $true; HippocampusCmd = $ManagedShim; PluginWheel = $plugTarget }
}

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------

$py = Resolve-Python
if (-not $py) {
    Write-Host "FAIL: a usable Python 3.10 / 3.11 / 3.12 is not on PATH." -ForegroundColor Red
    Write-Host "      Install Python from https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "      (the 'py' launcher is recommended; tick 'Add python.exe to PATH' in the installer)." -ForegroundColor Red
    exit 10
}
Write-Host "[installer] python $($py.Ver) found ($($py.Cmd))." -ForegroundColor Green

$uvPath = Resolve-Uv
if (-not $uvPath) {
    $uvPath = Install-Uv
    if (-not $uvPath) {
        exit 11
    }
}
Write-Host "[installer] uv found at $uvPath." -ForegroundColor Green

# Make sure v3-core is importable from the python we're going to call.
# If the user installed v3-core into a venv, the PS session won't have it on PATH;
# we still proceed because `hippocampus install` re-runs the preflight inside
# Python and will FAIL with a clear message if v3-core is not importable.

$docker = Resolve-Docker
if (-not $docker.Present) {
    Write-Host ""
    Write-Host "WARN: Docker is not installed." -ForegroundColor Yellow
    Write-Host "      The installer needs Docker Desktop to run a disposable pgvector container." -ForegroundColor Yellow
    Write-Host "      Install: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
    Write-Host "      The Python module will FAIL with the same actionable message if Docker is still missing." -ForegroundColor Yellow
    Write-Host ""
} elseif (-not $docker.Running) {
    Write-Host ""
    Write-Host "WARN: Docker is installed but the daemon is not responding." -ForegroundColor Yellow
    Write-Host "      Start Docker Desktop and re-run this installer." -ForegroundColor Yellow
    Write-Host ""
} else {
    Write-Host "[installer] docker daemon reachable." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 2. Delegate to `hippocampus install`
# ---------------------------------------------------------------------------

$hippo = Resolve-Hippocampus -PythonCmd $py.Cmd
if (-not $hippo) {
    Write-Host "[installer] hippocampus is not installed yet — installing the release package..." -ForegroundColor Cyan
    $installed = Install-Hippocampus -UvPath $uvPath
    if (-not $installed.Ok) {
        Write-Host ""
        Write-Host "FAIL: could not install the Hippocampus release package." -ForegroundColor Red
        Write-Host "      $($installed.Reason)" -ForegroundColor Red
        Write-Host "      Manual fallback:" -ForegroundColor Red
        Write-Host "        uv venv `"$ManagedVenv`"" -ForegroundColor Red
        Write-Host "        uv pip install --python `"$ManagedPython`" <v3_core-*.whl> <v3_hermes_plugin-*.whl>" -ForegroundColor Red
        exit 12
    }
    $hippo = $installed.HippocampusCmd
    if ($installed.PluginWheel) { $PluginWheel = $installed.PluginWheel }
}
if (-not $hippo) {
    Write-Host "FAIL: hippocampus still not runnable after install." -ForegroundColor Red
    exit 12
}

Write-Host "[installer] delegating to: $hippo install ..." -ForegroundColor Cyan

$hargs = @("install",
    "--preset", $Preset,
    "--pg-port", "$PgPort"
)
if ($ProfileDir)     { $hargs += @("--profile-dir", $ProfileDir) }
if ($EmbedKey)       { $hargs += @("--embed-key", $EmbedKey) }
if ($LlmKey)         { $hargs += @("--llm-key", $LlmKey) }
if ($LlmBaseUrl)     { $hargs += @("--llm-base-url", $LlmBaseUrl) }
if ($LlmModel)       { $hargs += @("--llm-model", $LlmModel) }
if ($PluginWheel)    { $hargs += @("--plugin-wheel", $PluginWheel) }
if ($SkipSmoke)      { $hargs += @("--skip-smoke") }

# Invoke and capture exit code without throwing on non-zero.
$rc = 0
try {
    if ($hippo -like "*-m v3core.distribution_cli*") {
        $tokens = $hippo.Split(" ")
        & $tokens[0] $tokens[1] @hargs
        $rc = $LASTEXITCODE
    } else {
        & $hippo @hargs
        $rc = $LASTEXITCODE
    }
} catch {
    Write-Host ""
    Write-Host "FAIL: installer raised an exception: $_" -ForegroundColor Red
    $rc = 99
}

# ---------------------------------------------------------------------------
# 3. Verdict echo
# ---------------------------------------------------------------------------

Write-Host ""
if ($rc -eq 0) {
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "  Hippocampus install: SUCCESS" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "  1. Restart the Hermes Agent process so the new memory provider takes effect."
    Write-Host "  2. Verify with: hippocampus doctor --static"
    Write-Host "  3. Verify a real write+readback with: hippocampus install --skip-smoke false  (already done)"
} else {
    Write-Host "========================================================" -ForegroundColor Red
    Write-Host "  Hippocampus install: FAILURE (exit $rc)" -ForegroundColor Red
    Write-Host "========================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "Re-run with -Help for the full list of options." -ForegroundColor Yellow
    Write-Host "The Python module's verdict block (printed above) names the failed step." -ForegroundColor Yellow
}

exit $rc
