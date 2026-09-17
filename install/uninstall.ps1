<#
.SYNOPSIS
    Hippocampus v0.2 First User Release - uninstaller.

.DESCRIPTION
    Reverses the install.  Removes the disposable pgvector Docker
    container, restores the Hermes config.yaml from the most recent
    backup, and (optionally) deletes the user profile directory.

    Designed to be run as:

        irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/uninstall.ps1 | iex

    Idempotent: re-running never fails; it reports what was already gone.

    Safe-by-default: the profile directory and the Hermes config backup
    file are NOT deleted unless the caller asks explicitly.  The default
    is "stop the container, restore the most recent Hermes config backup,
    leave profile + backups in place for inspection".

.PARAMETER ContainerName
    The Docker container name to remove.  Default: hippocampus-pg.

.PARAMETER HermesHome
    Override HERMES_HOME.  Default: $env:LOCALAPPDATA\hermes.

.PARAMETER RemoveProfile
    Delete the profile directory entirely.  DESTRUCTIVE: this deletes
    every config.yaml + backup under the profile dir.

.PARAMETER RemoveBackups
    Also delete the Hermes config.yaml.bak.* backups (only meaningful
    when RestoreHermesConfig is omitted / fails).

.PARAMETER RestoreHermesConfig
    After stopping the container, restore Hermes config.yaml from the
    most recent timestamped backup.  Default: $true.

.PARAMETER Yes
    Skip the interactive confirmations.

.EXAMPLE
    iex (irm .../uninstall.ps1)
    # Removes the hippocampus-pg container; restores the Hermes config from backup.

.EXAMPLE
    iex (irm .../uninstall.ps1) -RemoveProfile -Yes
    # Removes the container AND deletes the profile directory (destructive).
#>

[CmdletBinding()]
param(
    [string]$ContainerName = "hippocampus-pg",
    [string]$HermesHome = "",
    [switch]$RemoveProfile,
    [switch]$RemoveBackups,
    [bool]$RestoreHermesConfig = $true,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Hippocampus uninstaller" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Resolve-HermesHome {
    param([string]$Explicit)
    if ($Explicit) { return (Resolve-Path $Explicit).Path }
    $envHome = $env:HERMES_HOME
    if ($envHome) { return (Resolve-Path $envHome).Path }
    if ($env:LOCALAPPDATA) {
        $candidate = Join-Path $env:LOCALAPPDATA "hermes"
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    return $null
}

function Confirm-Or-Exit {
    param([string]$Message)
    if ($Yes) { return }
    $ans = Read-Host "$Message [y/N]"
    if ($ans -ne "y" -and $ans -ne "Y") {
        Write-Host "Aborted by user." -ForegroundColor Yellow
        exit 0
    }
}

# ---------------------------------------------------------------------------
# 1. Container
# ---------------------------------------------------------------------------

$dk = Get-Command "docker" -ErrorAction SilentlyContinue
if ($dk) {
    $running = & docker ps -a --format "{{.Names}}" 2>$null
    if ($running -and ($running -split "`n") -contains $ContainerName) {
        Confirm-Or-Exit "Remove Docker container '$ContainerName'?"
        Write-Host "[uninstall] stopping and removing $ContainerName ..."
        & docker rm -f $ContainerName | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[uninstall] container removed." -ForegroundColor Green
        } else {
            Write-Host "[uninstall] docker rm failed (rc=$LASTEXITCODE); continuing." -ForegroundColor Yellow
        }
    } else {
        Write-Host "[uninstall] container '$ContainerName' not present; skipping."
    }

    # Also try to remove the image (best effort).
    try {
        $imgId = & docker images --format "{{.ID}}" pgvector/pgvector:pg17 2>$null
        if ($imgId) {
            Write-Host "[uninstall] removing pgvector/pgvector:pg17 image (id=$imgId)..."
            & docker rmi -f $imgId 2>$null | Out-Null
        }
    } catch { }
} else {
    Write-Host "[uninstall] docker not on PATH; skipping container cleanup."
}

# ---------------------------------------------------------------------------
# 2. Hermes config restore
# ---------------------------------------------------------------------------

if ($RestoreHermesConfig) {
    $home = Resolve-HermesHome -Explicit $HermesHome
    if (-not $home) {
        Write-Host "[uninstall] Hermes home not found; skipping config restore." -ForegroundColor Yellow
    } else {
        $cfg = Join-Path $home "config.yaml"
        if (-not (Test-Path $cfg)) {
            Write-Host "[uninstall] Hermes config not found at $cfg; skipping restore." -ForegroundColor Yellow
        } else {
            # Find the newest timestamped backup.
            $pattern = "config.yaml.bak.*"
            $candidates = Get-ChildItem -Path $home -Filter $pattern -ErrorAction SilentlyContinue
            if (-not $candidates -or $candidates.Count -eq 0) {
                Write-Host "[uninstall] no Hermes config backups found; leaving $cfg unchanged." -ForegroundColor Yellow
            } else {
                $latest = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
                Confirm-Or-Exit "Restore $cfg from $($latest.Name)?"
                try {
                    Copy-Item -Path $latest.FullName -Destination $cfg -Force
                    Write-Host "[uninstall] Hermes config restored from $($latest.Name)." -ForegroundColor Green
                } catch {
                    Write-Host "[uninstall] restore failed: $_" -ForegroundColor Red
                }
                if ($RemoveBackups) {
                    Confirm-Or-Exit "Delete $($candidates.Count) backup file(s)?"
                    foreach ($b in $candidates) {
                        try { Remove-Item $b.FullName -Force } catch { }
                    }
                    Write-Host "[uninstall] $($candidates.Count) backup(s) deleted." -ForegroundColor Green
                }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 3. Optional profile dir delete
# ---------------------------------------------------------------------------

$profileDir = Join-Path $env:USERPROFILE ".v3-core\profiles\default"
if ($RemoveProfile -and (Test-Path $profileDir)) {
    Confirm-Or-Exit "DELETE profile directory $profileDir (DESTRUCTIVE)?"
    try {
        Remove-Item $profileDir -Recurse -Force
        Write-Host "[uninstall] profile directory deleted." -ForegroundColor Green
    } catch {
        Write-Host "[uninstall] profile delete failed: $_" -ForegroundColor Red
    }
} else {
    Write-Host "[uninstall] profile directory left in place at $profileDir."
    Write-Host "              Remove manually with: Remove-Item -Recurse -Force '$profileDir'"
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Uninstall complete." -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
