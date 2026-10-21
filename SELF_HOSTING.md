# Moving from Railway to this laptop

State to preserve = `data/database.sqlite` (history, drafts, costs, opens, replies,
suppressions) + `data/screenshots/`. Leads/CRM live in Google Sheets and need no move.

## 0. One-time laptop setup
1. Admin PowerShell: `wsl --install` then reboot.
2. Install Docker Desktop (WSL2 backend). Settings > General: "Start Docker Desktop when you sign in".
3. `%UserProfile%\.wslconfig`:
   ```
   [wsl2]
   memory=6GB
   processors=4
   ```
   then `wsl --shutdown`.
4. Power & sleep: sleep = Never (plugged in), lid close = do nothing.
   Settings > Time: "Set time automatically" ON.

## 1. Export the data from Railway (do this BEFORE cutting over)
Install the CLI and log in (opens a browser):
```
npm i -g @railway/cli
railway login
railway link        # pick the project + the WEB service
```
Make a consistent snapshot (a plain copy of a live SQLite file can be torn), then pull it:
```
railway ssh "python -c \"import sqlite3;s=sqlite3.connect('/app/data/database.sqlite');d=sqlite3.connect('/tmp/snap.sqlite');s.backup(d)\""
railway ssh "base64 /tmp/snap.sqlite" > snap.b64
```
Decode to `data/database.sqlite` here:
```
[IO.File]::WriteAllBytes("data\database.sqlite",[Convert]::FromBase64String((Get-Content snap.b64 -Raw)))
```
Screenshots are only needed for old drafts' images; tar them the same way if wanted.
Verify: `python -c "import sqlite3;c=sqlite3.connect('data/database.sqlite');print(c.execute('select count(*) from email_history').fetchone())"`

## 2. Configure
- Copy every variable from Railway (Variables tab > Raw Editor) into `.env`.
- Set `APP_BASE_URL` to the Funnel URL from step 3.
- Put `credentials.json` next to `docker-compose.yml` (or keep GOOGLE_CREDENTIALS_JSON in `.env`).
  If you use the env var, create an empty `credentials.json` file so the mount doesn't fail.

## 3. Public HTTPS via Tailscale Funnel (free, no domain)
1. Install Tailscale for Windows (tailscale.com/download), sign in.
2. Admin console > DNS: enable MagicDNS and HTTPS certificates. Access controls: allow Funnel for this device.
3. After the stack is up: `tailscale funnel --bg 8000`
4. `tailscale funnel status` shows the public URL, e.g. https://<pc>.<tailnet>.ts.net
5. Put that URL in .env as APP_BASE_URL, then `docker compose up -d` again.
To use a real domain later, only APP_BASE_URL changes.
## 4. Start, verify, then cut over
```
docker compose up -d --build
docker compose logs -f web scheduler
```
Check the Funnel URL, History shows old sends, `/unsubscribe` and `/o/x.gif` respond.
Only then: in Railway **stop the scheduler service first**, then the web service.
Keep Railway's volume for ~2 weeks as a backup before deleting it.

## Updating later
`git pull; docker compose up -d --build`

## Backups (do this â€” a laptop disk is a single point of failure)
Schedule a daily copy of `data/` to Google Drive/another disk. Use the sqlite `.backup`
command rather than copying the live file.

