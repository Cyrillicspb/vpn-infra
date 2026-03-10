#Requires -Version 5.1
<#
.SYNOPSIS
    VPN Infrastructure - Windows Installer
.DESCRIPTION
    Connects to your home Ubuntu server and runs setup.sh
#>

$ErrorActionPreference = 'Stop'

# ── Banner ────────────────────────────────────────────────────────────────────

Clear-Host
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "    VPN Infrastructure -- Windows Setup" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script will:"
Write-Host "    1. Generate an SSH key (ed25519)"
Write-Host "    2. Copy it to your home server"
Write-Host "    3. Run setup.sh on the server"
Write-Host ""
Write-Host "  Requirements:"
Write-Host "    - Windows 10/11"
Write-Host "    - Home server: Ubuntu Server 24.04"
Write-Host "    - Server connected to router via Ethernet"
Write-Host ""
Read-Host "  Press Enter to start"

# ── Check SSH ─────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  Checking SSH..." -NoNewline
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host " NOT FOUND" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Install OpenSSH Client:" -ForegroundColor Yellow
    Write-Host "    Settings -> Apps -> Optional features -> Add -> OpenSSH Client"
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Host " OK" -ForegroundColor Green

if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
    Write-Host "  [ERROR] ssh-keygen not found." -ForegroundColor Red
    Read-Host "  Press Enter to exit"
    exit 1
}

# ── Collect server info ───────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ---- Server connection ----"
Write-Host ""

do {
    $ServerIP = Read-Host "  Home server IP (e.g. 192.168.1.100)"
} while ([string]::IsNullOrWhiteSpace($ServerIP))

$ServerUser = Read-Host "  SSH username [sysadmin]"
if ([string]::IsNullOrWhiteSpace($ServerUser)) { $ServerUser = "sysadmin" }

$SshPortStr = Read-Host "  SSH port [22]"
if ([string]::IsNullOrWhiteSpace($SshPortStr)) { $SshPort = "22" } else { $SshPort = $SshPortStr }

Write-Host ""
Write-Host "  Target: $ServerUser@$ServerIP port $SshPort" -ForegroundColor Cyan
Write-Host ""

# ── SSH key ───────────────────────────────────────────────────────────────────

$SshDir = Join-Path $env:USERPROFILE ".ssh"
$SshKey = Join-Path $SshDir "vpn_deploy_key"
$SshKeyPub = "$SshKey.pub"

if (-not (Test-Path $SshKey)) {
    Write-Host "  Generating SSH key..." -NoNewline
    if (-not (Test-Path $SshDir)) { New-Item -ItemType Directory -Path $SshDir | Out-Null }
    & ssh-keygen -t ed25519 -f $SshKey -N '""' -C "vpn-deploy" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK" -ForegroundColor Green
        Write-Host "  Key: $SshKey"
    } else {
        Write-Host " FAILED" -ForegroundColor Red
        # Try without quotes around empty passphrase
        & ssh-keygen -t ed25519 -f $SshKey -N "" -C "vpn-deploy"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [ERROR] Could not generate SSH key." -ForegroundColor Red
            Read-Host "  Press Enter to exit"
            exit 1
        }
        Write-Host "  Key: $SshKey" -ForegroundColor Green
    }
} else {
    Write-Host "  SSH key already exists: $SshKey" -ForegroundColor Green
}

$PubKey = Get-Content $SshKeyPub -Raw
$PubKey = $PubKey.Trim()

# ── Copy public key to server ─────────────────────────────────────────────────

Write-Host ""
Write-Host "  Copying public key to server..."
Write-Host "  (Enter password for $ServerUser@$ServerIP when prompted)"
Write-Host ""

$CopyCmd = "mkdir -p ~/.ssh && echo '$PubKey' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo KEY_OK"
& ssh -o StrictHostKeyChecking=accept-new -p $SshPort "${ServerUser}@${ServerIP}" $CopyCmd

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  [WARNING] Could not copy key automatically." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Add key manually on server:" -ForegroundColor Yellow
    Write-Host "    1. Connect: ssh $ServerUser@$ServerIP -p $SshPort"
    Write-Host "    2. Run: mkdir -p ~/.ssh && nano ~/.ssh/authorized_keys"
    Write-Host "    3. Paste this key:"
    Write-Host ""
    Write-Host "  $PubKey" -ForegroundColor Gray
    Write-Host ""
    Read-Host "  Press Enter when done (or Enter to try anyway)"
}

# ── Test connection ───────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  Testing SSH connection..." -NoNewline
$TestResult = & ssh -i $SshKey -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p $SshPort "${ServerUser}@${ServerIP}" "echo OK_CONNECTION" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host " FAILED" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Cannot connect to server." -ForegroundColor Red
    Write-Host "  Check:"
    Write-Host "    1. Server is powered on and network-connected"
    Write-Host "    2. IP: $ServerIP  User: $ServerUser  Port: $SshPort"
    Write-Host "    3. OpenSSH server is installed: sudo apt install openssh-server"
    Write-Host ""
    Write-Host "  Try manually: ssh $ServerUser@$ServerIP -p $SshPort"
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Host " OK" -ForegroundColor Green

# ── Confirm and run ───────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "    Server ready. Starting installation." -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  WARNING: Installation takes 20-40 minutes." -ForegroundColor Yellow
Write-Host "           Do NOT close this window!" -ForegroundColor Yellow
Write-Host ""
$Confirm = Read-Host "  Start installation? (y/N)"
if ($Confirm -notmatch '^[Yy]$') {
    Write-Host "  Cancelled."
    Read-Host "  Press Enter to exit"
    exit 0
}

Write-Host ""
Write-Host "  Connecting to $ServerUser@$ServerIP ..."
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""

# ── Prepare remote script ─────────────────────────────────────────────────────
# Here-strings must start at column 0 — define them at top level, not in blocks.

# Script used when setup.sh is uploaded via scp (private repo / local copy)
$ScriptRunOnly = "chmod +x /tmp/vpn-setup.sh && echo '=== Running setup.sh ===' && sudo bash /tmp/vpn-setup.sh 2>&1 | tee /tmp/vpn-setup.log"

# Script used when setup.sh must be downloaded (public repo)
$ScriptDownload = @'
set -e
command -v curl >/dev/null 2>&1 || sudo apt-get install -y -qq curl
echo "=== Downloading setup.sh ==="
URLS=(
  "https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/setup.sh"
  "https://cdn.jsdelivr.net/gh/Cyrillicspb/vpn-infra@master/setup.sh"
)
downloaded=0
for url in "${URLS[@]}"; do
  echo "Trying: $url"
  if curl -sf --max-time 30 "$url" -o /tmp/vpn-setup.sh; then
    echo "[OK] Downloaded"
    downloaded=1
    break
  else
    echo "[WARN] Failed: $url"
  fi
done
if [ "$downloaded" -eq 0 ]; then
  echo "[ERROR] Could not download setup.sh."
  echo "  Repo may be private. Run from repo folder or make repo public."
  exit 1
fi
chmod +x /tmp/vpn-setup.sh
echo "=== Running setup.sh ==="
sudo bash /tmp/vpn-setup.sh 2>&1 | tee /tmp/vpn-setup.log
'@

# ── Upload setup.sh from local repo if available ──────────────────────────────
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$LocalSetup = Join-Path $RepoRoot "setup.sh"
$ScpOk      = $false

if (Test-Path $LocalSetup) {
    Write-Host "  Uploading setup.sh from local repo..." -NoNewline
    & scp -i $SshKey -P $SshPort -o StrictHostKeyChecking=accept-new `
          $LocalSetup "${ServerUser}@${ServerIP}:/tmp/vpn-setup.sh" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK" -ForegroundColor Green
        $ScpOk = $true
    } else {
        Write-Host " FAILED (will try download)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  setup.sh not found locally — will download on server."
}

$Script = if ($ScpOk) { $ScriptRunOnly } else { $ScriptDownload }

$Encoded   = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Script))
$RemoteCmd = "echo $Encoded | base64 -d | bash"

& ssh -i $SshKey `
      -o StrictHostKeyChecking=accept-new `
      -o ServerAliveInterval=30 `
      -o ServerAliveCountMax=10 `
      -p $SshPort `
      -t "${ServerUser}@${ServerIP}" `
      $RemoteCmd

$SetupResult = $LASTEXITCODE

# ── Result ────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan

if ($SetupResult -eq 0) {
    Write-Host ""
    Write-Host "  [OK] Installation completed!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor Cyan
    Write-Host "    1. Router Port Forwarding:"
    Write-Host "         UDP 51820 -> ${ServerIP}:51820  (AmneziaWG)"
    Write-Host "         UDP 51821 -> ${ServerIP}:51821  (WireGuard)"
    Write-Host "    2. Open Telegram -> send /start to your bot"
    Write-Host "    3. Import config into WireGuard / AmneziaWG"
    Write-Host ""
    Write-Host "  SSH access:" -ForegroundColor Gray
    Write-Host "    ssh -i `"$SshKey`" -p $SshPort $ServerUser@$ServerIP"
} else {
    Write-Host ""
    Write-Host "  [ERROR] Installation failed (exit code $SetupResult)" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Diagnostics:"
    Write-Host "    ssh -i `"$SshKey`" -p $SshPort $ServerUser@$ServerIP"
    Write-Host "    cat /tmp/vpn-setup.log"
    Write-Host ""
    Write-Host "  Re-run is safe -- completed steps are skipped automatically."
}

Write-Host ""
Read-Host "  Press Enter to exit"
