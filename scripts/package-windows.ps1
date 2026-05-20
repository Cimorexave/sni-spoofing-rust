<#
.SYNOPSIS
    Packages the Windows auto-runner distribution zip for sni-spoof-rs.

.DESCRIPTION
    Creates a versioned zip containing:
      - sni-spoof-rs.exe          (the core Rust binary)
      - WinDivert64.sys           (WinDivert kernel driver)
      - WinDivert.dll             (WinDivert user-mode DLL)
      - auto-runner.bat           (entry point batch script)
      - config.json               (default config)
      - cfgs.txt                  (SNI config list, if exists)
      - README.txt                (quick-start instructions)

    The script looks for the binary in this order:
      1. target/release/sni-spoof-rs.exe
      2. bins/sni-spoof-rs-windows-x64.exe
      3. releases/sni-spoof-rs-windows-amd64.zip (extracts from there)

.PARAMETER Version
    Version string for the zip filename (e.g., "v0.5.1").
    Default: "v0.0.0-dev"

.PARAMETER OutputDir
    Directory where the zip will be created.
    Default: "dist"

.PARAMETER WinDivertZip
    Path to the upstream Windows release zip containing WinDivert files.
    Default: "releases/sni-spoof-rs-windows-amd64.zip"

.EXAMPLE
    .\scripts\package-windows.ps1 -Version v0.5.1
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$Version = "v0.0.0-dev",

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = "dist",

    [Parameter(Mandatory = $false)]
    [string]$WinDivertZip = "releases/sni-spoof-rs-windows-amd64.zip"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path "$ScriptDir\.."

# ---------------------------------------------------------------------------
# Step 1: Locate the sni-spoof-rs.exe binary
# ---------------------------------------------------------------------------
$binarySource = $null
$possiblePaths = @(
    "$RepoRoot\target\release\sni-spoof-rs.exe",
    "$RepoRoot\bins\sni-spoof-rs-windows-x64.exe"
)

foreach ($p in $possiblePaths) {
    if (Test-Path $p) {
        $binarySource = $p
        Write-Host "[+] Found binary at: $p"
        break
    }
}

# If not found directly, try extracting from the upstream release zip
if (-not $binarySource -and (Test-Path $WinDivertZip)) {
    Write-Host "[*] Binary not found directly. Extracting from $WinDivertZip ..."
    $tempDir = "$RepoRoot\tmp_extract"
    if (Test-Path $tempDir) { Remove-Item -Recurse -Force $tempDir }
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $WinDivertZip))
    foreach ($entry in $zip.Entries) {
        if ($entry.Name -eq "sni-spoof-rs.exe") {
            $dest = "$tempDir\$($entry.Name)"
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            $binarySource = $dest
            Write-Host "[+] Extracted binary from zip to: $dest"
            break
        }
    }
    $zip.Dispose()
}

if (-not $binarySource) {
    Write-Error "ERROR: Could not find sni-spoof-rs.exe. Build it first with: cargo build --release"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 2: Locate WinDivert files
# ---------------------------------------------------------------------------
$windivertDir = "$RepoRoot\tmp_windivert"
if (Test-Path $windivertDir) { Remove-Item -Recurse -Force $windivertDir }
New-Item -ItemType Directory -Path $windivertDir -Force | Out-Null

if (Test-Path $WinDivertZip) {
    Write-Host "[+] Extracting WinDivert files from $WinDivertZip ..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $WinDivertZip))
    foreach ($entry in $zip.Entries) {
        $name = $entry.Name
        if ($name -match '^(WinDivert64\.sys|WinDivert\.dll)$') {
            $dest = "$windivertDir\$name"
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            Write-Host "    Extracted: $name"
        }
    }
    $zip.Dispose()
} else {
    Write-Warning "WARNING: $WinDivertZip not found. WinDivert files will be missing from the package."
}

# ---------------------------------------------------------------------------
# Step 3: Prepare the staging directory
# ---------------------------------------------------------------------------
$stageDir = "$RepoRoot\tmp_stage"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

# Copy binary
Copy-Item $binarySource "$stageDir\sni-spoof-rs.exe" -Force
Write-Host "[+] Copied: sni-spoof-rs.exe"

# Copy WinDivert files (if found)
if (Test-Path "$windivertDir\WinDivert64.sys") {
    Copy-Item "$windivertDir\WinDivert64.sys" "$stageDir\WinDivert64.sys" -Force
    Write-Host "[+] Copied: WinDivert64.sys"
}
if (Test-Path "$windivertDir\WinDivert.dll") {
    Copy-Item "$windivertDir\WinDivert.dll" "$stageDir\WinDivert.dll" -Force
    Write-Host "[+] Copied: WinDivert.dll"
}

# Copy auto-runner files
$runnerFiles = @(
    @{src = "runner\auto-runner.bat"; dst = "auto-runner.bat"},
    @{src = "runner\config.json"; dst = "config.json"}
)

foreach ($f in $runnerFiles) {
    $srcPath = "$RepoRoot\$($f.src)"
    if (Test-Path $srcPath) {
        Copy-Item $srcPath "$stageDir\$($f.dst)" -Force
        Write-Host "[+] Copied: $($f.dst)"
    } else {
        Write-Warning "WARNING: $($f.src) not found, skipping."
    }
}

# Copy cfgs.txt if it exists (it's gitignored, so may not be present)
if (Test-Path "$RepoRoot\runner\cfgs.txt") {
    Copy-Item "$RepoRoot\runner\cfgs.txt" "$stageDir\cfgs.txt" -Force
    Write-Host "[+] Copied: cfgs.txt"
} else {
    Write-Host "[*] cfgs.txt not found (it's gitignored). Users will need to provide their own."
}

# ---------------------------------------------------------------------------
# Step 4: Create README.txt
# ---------------------------------------------------------------------------
$readmeContent = @"
============================================
  SNI Spoof Auto-Runner for Windows
  Version: $Version
============================================

QUICK START:
  1. Right-click 'auto-runner.bat' → Run as Administrator
  2. The runner will find a working Cloudflare IP and SNI
  3. It will show your V2Ray config in a popup
  4. It will launch sni-spoof-rs.exe automatically
  5. Connect with your v2ray/xray client to 127.0.0.1:40443

REQUIREMENTS:
  - Windows 10/11 (64-bit)
  - Administrator privileges (required for WinDivert driver)

FILES IN THIS PACKAGE:
  - sni-spoof-rs.exe     - The core DPI bypass proxy
  - WinDivert64.sys      - WinDivert kernel driver
  - WinDivert.dll        - WinDivert user-mode library
  - auto-runner.bat      - Entry point (double-click this)
  - config.json          - Proxy configuration
  - cfgs.txt             - Your V2Ray/Trojan configs (add your own)

CUSTOMIZATION:
  Edit cfgs.txt to add your own V2Ray/Trojan subscription configs.
  The runner will test each one and pick the first working config.

TROUBLESHOOTING:
  - Make sure you run as Administrator
  - If your antivirus flags WinDivert64.sys, add an exclusion
  - Check RUST_LOG=debug for verbose logging

SOURCE & UPDATES:
  https://github.com/YOUR_USER/sni-spoof-rs

"@

$readmePath = "$stageDir\README.txt"
$readmeContent | Out-File -FilePath $readmePath -Encoding ASCII
Write-Host "[+] Created: README.txt"

# ---------------------------------------------------------------------------
# Step 5: Create the zip
# ---------------------------------------------------------------------------
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$zipName = "sni-spoof-rs-windows-amd64-auto-$Version.zip"
$zipPath = "$OutputDir\$zipName"

# Remove old zip if it exists
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stageDir, $zipPath)

Write-Host ""
Write-Host "[SUCCESS] Package created: $zipPath"
Write-Host "         Size: $((Get-Item $zipPath).Length / 1MB -as [int]) MB"

# ---------------------------------------------------------------------------
# Step 6: Cleanup temp directories
# ---------------------------------------------------------------------------
Remove-Item -Recurse -Force $stageDir
if (Test-Path $windivertDir) { Remove-Item -Recurse -Force $windivertDir }
if (Test-Path "$RepoRoot\tmp_extract") { Remove-Item -Recurse -Force "$RepoRoot\tmp_extract" }

Write-Host "[+] Temp files cleaned up."
