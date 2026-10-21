# Auto-update for the laptop server (Windows). Run every 5 minutes by Task Scheduler.
# Edit $Repo to the folder that holds docker-compose.yml and this git checkout.
$Repo = "C:\path\to\lead-audit-bot"
Set-Location $Repo
git fetch origin main 2>$null
$local  = (git rev-parse HEAD).Trim()
$remote = (git rev-parse origin/main).Trim()
if ($local -ne $remote) {
    Write-Output "$(Get-Date -Format s) updating $local -> $remote"
    git pull --ff-only origin main
    if ($LASTEXITCODE -eq 0) { docker compose up -d --build }
}
