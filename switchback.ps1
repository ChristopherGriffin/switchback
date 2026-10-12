#requires -version 5
<#
  switchback.ps1 - run the whole SwitchBot Plug Mini -> ESPHome flasher from this one PC.

  It serves our firmware and answers DNS for wohand.com, so any Plug Mini whose
  DNS points at this PC fetches our firmware instead of SwitchBot's. Then the
  batch tool triggers each plug over Bluetooth.

  Usage (PowerShell; if a port is refused, use an elevated window):
      .\switchback.ps1               serve the portal payload + DNS, then wait
      .\switchback.ps1 -Implant      bake your Wi-Fi into the plugs (needs secrets.yaml)
      .\switchback.ps1 -Flash        also open the Bluetooth flasher once the server is up
      .\switchback.ps1 -NoDns        server only (you already redirect wohand.com elsewhere)
      .\switchback.ps1 -Rebuild      rebuild the payload even if one is already staged

  Stop the server with Ctrl+C.
#>
[CmdletBinding()]
param(
    [switch]$Implant,
    [switch]$Flash,
    [switch]$NoDns,
    [switch]$Rebuild,
    [string]$Ip,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$esphomeDir = Join-Path $root 'esphome'
$serverDir = Join-Path $root 'server'
$toolsDir = Join-Path $root 'tools'
$secrets = Join-Path $esphomeDir 'secrets.yaml'
$payload = Join-Path $serverDir 'bin\payload.bin'
$appBin = Join-Path $serverDir 'bin\app.bin'
$variant = if ($Implant) { 'implant' } else { 'portal' }

$appBinUrl = 'https://github.com/ChristopherGriffin/switchback/releases/download/esphome-v0.1.0/app.bin'
$appBinSha256 = 'dc99cacdfe1a46debfdb66d971c44673ed957d8ebe8331f430e9016bbe5cbc56'

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

function Find-LanIp {
    if ($Ip) { return $Ip }
    # the IPv4 address on whichever interface holds the default route
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, @{e = { (Get-NetIPInterface -InterfaceIndex $_.ifIndex).InterfaceMetric } } |
        Select-Object -First 1
    if (-not $route) { Fail "Could not find this PC's network address. Pass it with -Ip 192.168.x.y" }
    $addr = Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127|169)\.' } | Select-Object -First 1
    if (-not $addr) { Fail "Could not read this PC's IPv4 address. Pass it with -Ip 192.168.x.y" }
    return $addr.IPAddress
}

function Find-EsphomePython {
    if ($Python) {
        if (& $Python -m esphome version 2>$null) { return $Python }
        Fail "ESPHome does not run under $Python"
    }
    # ESPHome needs Python 3.10 - 3.13. Try the common launchers, newest first.
    $cands = @()
    foreach ($v in '3.13', '3.12', '3.11', '3.10') { $cands += , @('py', "-$v") }
    $cands += , @('python') ; $cands += , @('python3')
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $pre = $c[1..($c.Length - 1)]
            if (& $exe @pre -m esphome version 2>$null) { return ($c -join ' ') }
        } catch { }
    }
    return $null
}

function Ensure-Secrets {
    if (-not (Test-Path $secrets)) {
        Copy-Item (Join-Path $esphomeDir 'secrets.yaml.example') $secrets
    }
    $text = Get-Content $secrets -Raw
    function Blank($name) { return $text -match "(?m)^\s*${name}\s*:\s*`"?`"?\s*$" -or $text -notmatch "(?m)^\s*${name}\s*:" }

    # The bootstrap just needs a key and OTA password. Generate them once so there is
    # nothing to fill in. They end up in secrets.yaml if you want to reuse them later.
    if (Blank 'api_encryption_key') {
        $key = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
        $text = $text -replace '(?m)^\s*api_encryption_key\s*:.*$', "api_encryption_key: `"$key`""
        Step "Generated a fresh API encryption key in secrets.yaml"
    }
    if (Blank 'ota_password') {
        $otap = -join ((1..24) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
        $text = $text -replace '(?m)^\s*ota_password\s*:.*$', "ota_password: `"$otap`""
        Step "Generated a fresh OTA password in secrets.yaml"
    }
    Set-Content -Path $secrets -Value $text -Encoding UTF8 -NoNewline

    if ($variant -eq 'implant') {
        $text = Get-Content $secrets -Raw
        if (($text -notmatch '(?m)^\s*wifi_ssid\s*:\s*\S') -or ($text -match '(?m)^\s*wifi_ssid\s*:\s*""')) {
            Fail "The implant build needs your Wi-Fi. Fill in wifi_ssid and wifi_password in`n  $secrets`nor run without -Implant to use the hotspot/portal build."
        }
    }
}

function Ensure-AppBin {
    if (Test-Path $appBin) { return }
    Step "Downloading the flasher (app.bin)"
    $tmp = "$appBin.tmp"
    try { Invoke-WebRequest -Uri $appBinUrl -OutFile $tmp -UseBasicParsing }
    catch { Fail "Could not download app.bin. Build it from the espressif/ folder, or grab it from the project's GitHub releases, and put it at`n  $appBin" }
    $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $appBinSha256) { Remove-Item $tmp; Fail "app.bin did not match its checksum. Nothing was installed." }
    Move-Item $tmp $appBin -Force
}

function Ensure-Payload {
    if ((Test-Path $payload) -and -not $Rebuild) { return }
    Ensure-Secrets
    $py = Find-EsphomePython
    if (-not $py) {
        Fail "No ESPHome found to build the payload. Install it (pip install esphome) on a Python 3.10-3.13, then run this again.`nOr build on another machine and copy its firmware.factory.bin to`n  $payload"
    }
    Step "Building the $variant payload with: $py  (first build downloads a toolchain and can take 15 minutes)"
    Push-Location $root
    try {
        $parts = $py -split ' '
        $exe = $parts[0]; $pre = if ($parts.Count -gt 1) { $parts[1..($parts.Count - 1)] } else { @() }
        & $exe @pre (Join-Path $toolsDir 'build_payload.py') $variant
        if ($LASTEXITCODE -ne 0) { Fail "The payload build failed. Nothing is staged." }
    } finally { Pop-Location }
}

function Ensure-ServerDeps {
    if (Test-Path (Join-Path $serverDir 'node_modules')) { return }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js is not installed. Get it from https://nodejs.org and run this again." }
    Step "Installing the server's dependencies"
    Push-Location $serverDir
    try { & npm ci --omit=dev; if ($LASTEXITCODE -ne 0) { & npm install --omit=dev } } finally { Pop-Location }
}

function Ensure-BatchVenv {
    # The Bluetooth flasher needs bleak and zeroconf, which do not belong in the
    # ESPHome environment. Keep them in their own .venv and reuse it.
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    $haveDeps = $false
    if (Test-Path $venvPy) { & $venvPy -c 'import bleak, zeroconf' 2>$null; $haveDeps = ($LASTEXITCODE -eq 0) }
    if (-not $haveDeps) {
        $base = $null
        foreach ($cand in 'python', 'py') { if (Get-Command $cand -ErrorAction SilentlyContinue) { $base = $cand; break } }
        if (-not $base) { Fail "Python is needed for the Bluetooth flasher. Install Python 3.9+ from https://python.org and run this again." }
        if (-not (Test-Path $venvPy)) {
            Step "Creating a Python environment for the Bluetooth flasher"
            & $base -m venv (Join-Path $root '.venv')
        }
        Step "Installing the flasher's packages (bleak, zeroconf)"
        & $venvPy -m pip install -q --upgrade pip
        & $venvPy -m pip install -q -r (Join-Path $toolsDir 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { Fail "Could not install the flasher's Python packages." }
    }
    return $venvPy
}

# ---- run ----
if (-not (Test-Path $serverDir)) { Fail "Run this from inside the switchback folder." }
$lanIp = Find-LanIp
Ensure-AppBin
Ensure-Payload
Ensure-ServerDeps

$md5 = (Get-FileHash $payload -Algorithm MD5).Hash.ToLower()
Step "Ready. This PC is $lanIp"
Write-Host "    payload: $variant, $((Get-Item $payload).Length) bytes, md5 $md5"

Write-Host @"

Point your plugs at this PC for DNS, one of:
  - set your router's DHCP DNS server to $lanIp (whole network, simplest), or
  - set just the plug's network/VLAN DNS to $lanIp, or
  - if you run your own DNS (Pi-hole, AdGuard, Technitium...), point
    wohand.com and www.wohand.com at $lanIp there and add -NoDns here.

Each plug must already be on Wi-Fi via the SwitchBot app before flashing.
"@ -ForegroundColor Yellow

$serverArgs = @('index.js')
if (-not $NoDns) { $serverArgs += $lanIp }

if ($Flash) {
    Step "Starting the server in the background"
    Push-Location $serverDir
    $proc = Start-Process node -ArgumentList $serverArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $serverDir 'server.log') -RedirectStandardError (Join-Path $serverDir 'server.err')
    Pop-Location
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep 1
        if ((Test-Path "$serverDir\server.log") -and (Select-String -Path "$serverDir\server.log" -Pattern 'Server listening' -Quiet)) { break }
        if ($proc.HasExited) { Get-Content "$serverDir\server.err" -Tail 10; Fail "The server exited on startup. Its log is above (often a busy port 80 or 53)." }
    }
    Step "Server up (pid $($proc.Id)). Logs: $serverDir\server.log"
    $batchPy = Ensure-BatchVenv
    Step "Opening the Bluetooth flasher. Close it to stop; the server keeps running (stop it with: Stop-Process -Id $($proc.Id))"
    & $batchPy (Join-Path $toolsDir 'sbplug_batch.py') --server "http://$lanIp" --skip-dns-check
} else {
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    $flashHint = if (Test-Path $venvPy) { "`"$venvPy`" tools\sbplug_batch.py" } else { "run .\switchback.ps1 -Flash, or set up tools\sbplug_batch.py yourself" }
    Step "Starting the server (Ctrl+C to stop). In another window, run the flasher:"
    Write-Host "    $flashHint --server http://$lanIp --skip-dns-check`n"
    Push-Location $serverDir
    try { & node @serverArgs } finally { Pop-Location }
}
