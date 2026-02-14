param(
  [string]$RepoPath = "",
  [int]$Port = 8787,
  [switch]$SkipCodexInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-RepoPath {
  param([string]$InputPath)

  if (![string]::IsNullOrWhiteSpace($InputPath)) {
    if (Test-Path $InputPath) { return (Resolve-Path $InputPath).Path }
    throw "RepoPath not found: $InputPath"
  }

  $scriptRepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  if (Test-Path (Join-Path $scriptRepoPath "scripts\codex_http_bridge.py")) {
    return $scriptRepoPath
  }

  $defaultWinPath = "C:\Users\$env:USERNAME\band-ai-dashboard"
  if (Test-Path $defaultWinPath) {
    return (Resolve-Path $defaultWinPath).Path
  }

  $distros = @()
  try {
    $distros = (& wsl -l -q) | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
  } catch {
    $distros = @()
  }

  foreach ($distro in $distros) {
    $cleanDistro = ($distro -replace "[^\w\.-]", "").Trim()
    if ([string]::IsNullOrWhiteSpace($cleanDistro)) { continue }
    $candidate = "\\wsl$\$cleanDistro\home\san\band-ai-dashboard"
    try {
      if (Test-Path $candidate) {
        return (Resolve-Path $candidate).Path
      }
    } catch {
      continue
    }
  }

  throw "Could not find band-ai-dashboard. Use -RepoPath to set it explicitly."
}

function Get-PythonCommand {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    return @{ File = "py"; Args = @("-3") }
  }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    return @{ File = "python"; Args = @() }
  }
  throw "Python not found. Install Python 3 first."
}

$resolvedRepoPath = Resolve-RepoPath -InputPath $RepoPath
$bridgeScript = Join-Path $resolvedRepoPath "scripts\codex_http_bridge.py"
if (!(Test-Path $bridgeScript)) {
  throw "Bridge script not found: $bridgeScript"
}

Write-Step "Repo path: $resolvedRepoPath"

if (!$SkipCodexInstall) {
  Write-Step "Installing @openai/codex globally"
  if (!(Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm not found. Install Node.js first."
  }

  & npm uninstall -g @openai/codex | Out-Host
  & npm install -g @openai/codex@latest | Out-Host

  $codexCmd = Get-Command codex -ErrorAction SilentlyContinue
  if ($codexCmd) {
    Write-Host "codex path: $($codexCmd.Source)"
    & codex --version | Out-Host
  } else {
    Write-Host "codex command not found in PATH after install." -ForegroundColor Yellow
  }

  $npmRoot = (& npm root -g).Trim()
  Write-Host "npm root -g: $npmRoot"

  if (Get-Command node -ErrorAction SilentlyContinue) {
    $platform = (& node -p "process.platform").Trim()
    $arch = (& node -p "process.arch").Trim()
    $expectedPackage = "codex-$platform-$arch"
    $candidatePaths = @(
      (Join-Path $npmRoot "@openai\$expectedPackage"),
      (Join-Path $npmRoot "@openai\codex\node_modules\@openai\$expectedPackage")
    )
    $foundPath = $candidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($foundPath) {
      Write-Host "runtime package found: $foundPath"
    } else {
      Write-Host "runtime package missing: $expectedPackage" -ForegroundColor Yellow
      Write-Host "Checked paths:" -ForegroundColor Yellow
      $candidatePaths | ForEach-Object { Write-Host " - $_" -ForegroundColor Yellow }
    }
  }
}

Write-Step "Configuring firewall rule for TCP $Port"
if (!(Get-Command netsh -ErrorAction SilentlyContinue)) {
  throw "netsh not found."
}

& netsh advfirewall firewall delete rule name="Codex Bridge $Port Any" | Out-Host
& netsh advfirewall firewall add rule name="Codex Bridge $Port Any" dir=in action=allow protocol=TCP localport=$Port profile=any | Out-Host

$python = Get-PythonCommand
$bridgeArgs = @()
$bridgeArgs += $python.Args
$bridgeArgs += @(
  $bridgeScript,
  "--repo-path", $resolvedRepoPath,
  "--host", "0.0.0.0",
  "--port", "$Port"
)

Write-Step "Starting bridge server"
$proc = Start-Process -FilePath $python.File -ArgumentList $bridgeArgs -WorkingDirectory $resolvedRepoPath -PassThru
Start-Sleep -Seconds 2

Write-Step "Health check: localhost"
try {
  & curl.exe --max-time 5 "http://127.0.0.1:$Port/health" | Out-Host
} catch {
  Write-Host "Local health check failed. Process id: $($proc.Id)" -ForegroundColor Yellow
}

if (Get-Command tailscale -ErrorAction SilentlyContinue) {
  $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
  if ($tsIp) {
    Write-Step "Health check: tailscale $tsIp"
    try {
      & curl.exe --max-time 5 "http://$tsIp`:$Port/health" | Out-Host
    } catch {
      Write-Host "Tailscale health check failed: http://$tsIp`:$Port/health" -ForegroundColor Yellow
    }
  }
}

Write-Step "Done"
Write-Host "Bridge process id: $($proc.Id)"
Write-Host "If you need to stop it: Stop-Process -Id $($proc.Id)"
