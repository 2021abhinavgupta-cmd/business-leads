# Updating the live link

One-time setup on the laptop that runs Docker:

1. Add to `.env` (or compose environment):
   `APP_BASE_URL=https://vidit-hppavilion.tailc74204.ts.net`
   `MAILING_ADDRESS=<company postal address>`
2. Update now: `git pull origin main` then `docker compose up -d --build`.
3. Auto-update after every push to main:
   - Windows: edit `$Repo` in `auto-update.ps1`, then in Task Scheduler create a task
     running `powershell -ExecutionPolicy Bypass -File <path>\deploy\auto-update.ps1`
     every 5 minutes.
   - Linux/Mac: `chmod +x deploy/auto-update.sh`, add the cron line shown inside it.
