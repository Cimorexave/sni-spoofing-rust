@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: ============================================================
:: CHECK: Administrator privileges
:: ============================================================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo This script requires Administrator privileges.
    echo Restarting with elevated permissions...
    powershell start -verb runas '%~f0'
    exit /b
)

:: ============================================================
:: CHECK: sni-spoof-rs.exe must be present
:: ============================================================
if not exist "sni-spoof-rs.exe" (
    echo ERROR: sni-spoof-rs.exe not found in the current directory.
    echo Make sure it is placed alongside auto-runner.bat.
    pause
    exit /b 1
)

echo ============================================
echo   SNI Spoof Auto-Runner
echo ============================================
echo.

:: ============================================================
:: STEP 1: Read current connect IP from config.json
:: ============================================================
echo [1] Reading current config from config.json...

:: Use PowerShell to properly parse JSON and extract just the IPv4 address
set "current_ip="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "$c = Get-Content 'config.json' -Raw | ConvertFrom-Json; $conn = $c.listeners[0].connect; if ([string]::IsNullOrEmpty($conn)) { exit } $ip = ($conn -split ':')[0]; Write-Output $ip"`) do set "current_ip=%%a"

set "current_port=443"
echo    Current IP: %current_ip%
echo    Current Port: %current_port%
echo.

:: ----------------------------------------------------------
:: Validate the extracted IP address (must be IPv4)
:: ----------------------------------------------------------
echo [1a] Validating IP address...

set "ip_valid=0"
for /f "usebackq delims=" %%r in (`powershell -NoProfile -Command "$ip='%current_ip%'; $parsed = $null; $ok = [System.Net.IPAddress]::TryParse($ip, [ref]$parsed); if (-not $ok) { exit 1 } if ($parsed.AddressFamily -ne 'InterNetwork') { exit 1 } exit 0"`) do set "dummy=%%r"
if %errorlevel% equ 0 (
    set "ip_valid=1"
    echo    IP '%current_ip%' is a valid IPv4 address.
) else (
    echo    WARNING: '%current_ip%' is not a valid IPv4 address (or is empty).
    echo    Will search for a working SNI from cfgs.txt...
    echo.
)
echo.

:: ============================================================
:: STEP 2: TCP connect test on the current IP (port 443)
::         Uses TCP instead of ICMP ping because many Cloudflare
::         IPs block ICMP but still accept TCP connections.
:: ============================================================
if "!ip_valid!"=="1" (
    echo [2] Testing TCP connectivity to %current_ip%:443...

    for /f "usebackq delims=" %%r in (`
        powershell -NoProfile -Command "$ip='%current_ip%'; $tcp = New-Object System.Net.Sockets.TcpClient; $conn = $tcp.BeginConnect($ip, 443, $null, $null); $wait = $conn.AsyncWaitHandle.WaitOne(3000, $false); if ($wait -and $tcp.Connected) { $tcp.EndConnect($conn); $tcp.Close(); exit 0 } else { $tcp.Close(); exit 1 }"
    `) do set "dummy=%%r"

    if !errorlevel! equ 0 (
        echo    SUCCESS: %current_ip%:443 is reachable (TCP).
        echo    Performing reverse lookup: finding config in cfgs.txt whose SNI resolves to %current_ip%...
        echo.
        
        powershell -Command "$lines = Get-Content 'cfgs.txt'; $target='%current_ip%'; $found = $null; foreach ($l in $lines) { if ($l -match '^trojan://') { $p = [regex]::Match($l, 'sni=([^&]+)'); $domain = if ($p.Success) { $p.Groups[1].Value } else { $null } } elseif ($l -match '^vless://') { $p = [regex]::Match($l, 'host=([^&]+)'); $domain = if ($p.Success) { $p.Groups[1].Value } else { $null } } else { $domain = $null }; if (-not $domain) { continue }; try { $ips = [System.Net.Dns]::GetHostAddresses($domain) } catch { $ips = @() }; foreach ($ip in $ips) { if ($ip.IPAddressToString -eq $target) { $found = $l; break } }; if ($found) { break } }; if ($found) { [System.IO.File]::WriteAllText('sni_config.tmp', $found, [System.Text.Encoding]::ASCII); exit 0 } else { exit 1 }"
        
        if !errorlevel! equ 0 (
            echo    Found matching config for IP %current_ip%.
            echo.
        ) else (
            echo    No config found in cfgs.txt whose SNI resolves to %current_ip%.
            echo.
        )
        goto :SHOW_POPUP
    ) else (
        echo    FAILED: %current_ip%:443 is not reachable (TCP timeout).
        echo    Will search for a working SNI from cfgs.txt...
        echo.
    )
) else (
    echo [2] Skipping TCP test - IP is empty or invalid.
    echo    Will search for a working SNI from cfgs.txt...
    echo.
)

:: ============================================================
:: STEP 3: Search cfgs.txt for a working SNI
:: ============================================================
echo [3] Searching cfgs.txt for a working SNI configuration...
echo.

if not exist cfgs.txt (
    echo ERROR: cfgs.txt not found!
    pause
    exit /b 1
)

for /f "usebackq delims=" %%R in (`
    powershell -Command "$lines = Get-Content 'cfgs.txt'; $result = $null; foreach ($l in $lines) { if ($l -match '^trojan://') { $p = [regex]::Match($l, 'sni=([^&]+)'); $domain = if ($p.Success) { $p.Groups[1].Value } else { $null } } elseif ($l -match '^vless://') { $p = [regex]::Match($l, 'host=([^&]+)'); $domain = if ($p.Success) { $p.Groups[1].Value } else { $null } } else { $domain = $null }; if (-not $domain) { continue }; Write-Host ('    Testing SNI: ' + $domain); Write-Host '       - Resolving...'; try { $ips = [System.Net.Dns]::GetHostAddresses($domain) } catch { $ips = @() }; if ($ips.Length -gt 0) { $ip = $ips[0].IPAddressToString; Write-Host ('       - Resolved to: ' + $ip); Write-Host '       - Testing TCP on port 443...'; $tcp = New-Object System.Net.Sockets.TcpClient; $conn = $tcp.BeginConnect($domain, 443, $null, $null); $wait = $conn.AsyncWaitHandle.WaitOne(3000, $false); if ($wait -and $tcp.Connected) { $tcp.EndConnect($conn); $tcp.Close(); Write-Host ('    SUCCESS: ' + $domain + ' is reachable on port 443.'); [System.IO.File]::WriteAllText('sni_config.tmp', $l, [System.Text.Encoding]::ASCII); $result = $ip; break } else { $tcp.Close(); Write-Host '       - Port 443 not reachable, trying next...' } } else { Write-Host '       - DNS resolution failed, trying next...' } }; if ($result) { Write-Output $result } else { Write-Output 'NONE' }"
`) do set "ps_result=%%R"

if "!ps_result!"=="NONE" (
    echo.
    echo ERROR: No working SNI configuration found in cfgs.txt.
    pause
    exit /b 1
)

set "new_ip=!ps_result!"
echo.

:: ============================================================
:: STEP 3c: Update config.json with the new IP
:: ============================================================
:UPDATE_CONFIG
echo [3c] Updating config.json with new IP: %new_ip%:443...

powershell -Command "$config = Get-Content 'config.json' -Raw | ConvertFrom-Json; $config.listeners[0].connect = '%new_ip%:443'; $config | ConvertTo-Json | Set-Content 'config.json' -Encoding UTF8"

if %errorlevel% neq 0 (
    echo ERROR: Failed to update config.json!
    pause
    exit /b 1
)
echo    config.json updated successfully.
echo.

:: ============================================================
:: STEP 4: Show the config to the user in a copyable popup
:: ============================================================
:SHOW_POPUP
echo [4] Displaying configuration to user...

if exist "sni_config.tmp" (
    powershell -Command "$c = [System.IO.File]::ReadAllText('sni_config.tmp'); Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.Form; $f.Text = 'V2Ray Config'; $f.Size = New-Object System.Drawing.Size(700,300); $f.StartPosition = 'CenterScreen'; $f.TopMost = $true; $l = New-Object System.Windows.Forms.Label; $l.Text = 'use this config in v2ray'; $l.Location = New-Object System.Drawing.Point(10,10); $l.Size = New-Object System.Drawing.Size(660,20); $f.Controls.Add($l); $t = New-Object System.Windows.Forms.TextBox; $t.Multiline = $true; $t.ReadOnly = $true; $t.Text = $c; $t.Location = New-Object System.Drawing.Point(10,35); $t.Size = New-Object System.Drawing.Size(660,180); $t.ScrollBars = 'Vertical'; $t.WordWrap = $false; $f.Controls.Add($t); $b = New-Object System.Windows.Forms.Button; $b.Text = 'OK'; $b.Location = New-Object System.Drawing.Point(310,225); $b.Size = New-Object System.Drawing.Size(80,30); $b.Add_Click({$f.Close()}); $f.Controls.Add($b); $f.ShowDialog() | Out-Null"
    del "sni_config.tmp" 2>nul
) else (
    echo    No matching config found to display.
    echo.
    powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('No matching V2Ray config found in cfgs.txt for the current IP.', 'SNI Spoof', 'OK', 'Information')"
)

echo.

:: ============================================================
:: STEP 5: Launch sni-spoof-rs.exe (foreground)
:: ============================================================
echo [5] Launching sni-spoof-rs.exe with config.json...
echo.

sni-spoof-rs.exe config.json
set "app_exit=%errorlevel%"

if %app_exit% neq 0 (
    echo.
    echo WARNING: sni-spoof-rs.exe exited with code %app_exit%.
)

echo.
echo Done.
pause
exit /b 0
