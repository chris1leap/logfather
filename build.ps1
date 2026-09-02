param(
    [string]$Version = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

if ($Clean) {
    if (Test-Path "build") {
        try {
            Remove-Item -Recurse -Force "build" -ErrorAction Stop
        } catch {
            $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
            Rename-Item -Path "build" -NewName ("build_old_" + $stamp)
        }
    }
    if (Test-Path "dist") {
        try {
            Remove-Item -Recurse -Force "dist" -ErrorAction Stop
        } catch {
            $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
            Rename-Item -Path "dist" -NewName ("dist_old_" + $stamp)
        }
    }
}

$buildDate = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if (-not $Version -or $Version -eq "0.0.0") {
    # Versions are 0.<commit count>: every commit bumps the number and maps
    # back to exactly one commit (see src/app_version.py).
    $commitCount = ""
    try {
        $commitCount = (git rev-list --count HEAD).Trim()
    } catch {
        $commitCount = ""
    }
    if ($commitCount -match '^\d+$') {
        $Version = "0.{0:D3}" -f [int]$commitCount
    } else {
        $Version = Get-Date -Format "yyyy.MM.dd.HHmm"
    }
}
$gitSha = ""
try {
    $gitSha = (git rev-parse --short HEAD).Trim()
} catch {
    $gitSha = ""
}

$versionInfo = @{
    version = $Version
    build_date = $buildDate
    git_sha = $gitSha
}
$versionInfo | ConvertTo-Json | Set-Content -Path "version.json" -Encoding ASCII

$env:APP_VERSION = $Version

# Pick an available spec file (prefer the tesseract variant if present).
$specFile = if (Test-Path "VideoLogViewer_tesseract.spec") {
    "VideoLogViewer_tesseract.spec"
} elseif (Test-Path "VideoLogViewer.spec") {
    "VideoLogViewer.spec"
} else {
    throw "No PyInstaller spec file found."
}

# Prefer local venv Python if available.
$pythonExe = if (Test-Path ".\.venv\Scripts\python.exe") {
    ".\.venv\Scripts\python.exe"
} else {
    "python"
}

# Ensure previous app instances do not lock dist output files.
$distExe = Join-Path (Join-Path (Get-Location) "dist\\The Logfather") "The Logfather.exe"
$running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and ($_.Path -ieq $distExe)
}
if ($running) {
    Write-Host "Stopping running app instance from dist output..."
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

$defaultDistRoot = Join-Path (Get-Location) "dist"
$defaultAppDist = Join-Path $defaultDistRoot "The Logfather"
$distPathArg = $defaultDistRoot

# Try to remove the previous dist output first to avoid PyInstaller lock failures.
if (Test-Path -LiteralPath $defaultAppDist) {
    $removed = $false
    for ($i = 0; $i -lt 8; $i++) {
        try {
            Remove-Item -LiteralPath $defaultAppDist -Recurse -Force -ErrorAction Stop
            $removed = $true
            break
        } catch {
            Start-Sleep -Milliseconds 750
        }
    }
    if (-not $removed -and (Test-Path -LiteralPath $defaultAppDist)) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $distPathArg = Join-Path $defaultDistRoot ("build_" + $stamp)
        New-Item -ItemType Directory -Path $distPathArg -Force | Out-Null
        Write-Warning "dist\\The Logfather is locked; building to alternate dist path: $distPathArg"
    }
}

& $pythonExe -m PyInstaller --noconfirm --distpath $distPathArg $specFile

# Build installer (requires Inno Setup's iscc.exe in PATH)
$isccCmd = Get-Command iscc -ErrorAction SilentlyContinue
if ($null -ne $isccCmd) {
    if ($distPathArg -ieq $defaultDistRoot) {
        & $isccCmd.Source "VideoLogViewer.iss"
    } else {
        Write-Warning "Installer build skipped because build output used alternate dist path."
        Write-Warning "Re-run when dist\\The Logfather is unlocked to build installer with VideoLogViewer.iss."
    }
} else {
    Write-Warning "Inno Setup compiler (iscc) not found in PATH. Skipping installer build."
    Write-Warning "Install Inno Setup and add iscc.exe to PATH to build the installer."
}
