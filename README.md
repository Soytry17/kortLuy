# Telegram money tracker

Personal Telegram bot for daily income and expenses. Logs amounts with categories, shows today/week/month totals and running balance, pings you each evening, and exports CSV.

## Create the bot

1. Open Telegram and chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a name and username.
3. Copy the token BotFather gives you (looks like `123456789:AA...`).
4. Get your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot).

## Run locally (Windows)

```powershell
cd D:\kortra
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

```
BOT_TOKEN=paste-token-from-BotFather
DATABASE_URL=sqlite:///data.db
ALLOWED_USER_IDS=your-numeric-telegram-id
TZ=Asia/Phnom_Penh
```

Then:

```powershell
python -m bot.main
```

Open Telegram, find your bot, send `/start`.

## Commands

| Command | Example |
|---|---|
| `/expense` | `/expense 4.50 food lunch` |
| `/income` | `/income 500 salary payday` |
| `/today` `/week` `/month` | Totals and entries |
| `/balance` | All-time income minus expense |
| `/categories` | List; add with `/categories add expense coffee` |
| `/undo` | Delete the last entry |
| `/export` | CSV file |
| `/remind` | `/remind 21:00` (timezone `Asia/Phnom_Penh`) |

If you omit amount, category, or note, the bot asks step by step.

## Deploy on Railway

1. Push this repo to GitHub.
2. In [Railway](https://railway.app), **New project** → **Deploy from GitHub repo**.
3. **Add plugin** → **PostgreSQL**. Railway sets `DATABASE_URL` automatically.
4. In the service **Variables**, set:
   - `BOT_TOKEN` — from BotFather
   - `ALLOWED_USER_IDS` — your Telegram user ID
   - `TZ` — `Asia/Phnom_Penh`
5. Deploy. The Dockerfile runs `alembic upgrade head` then starts polling.

Railway may set `PORT`; the bot binds to it so health checks pass. The bot itself uses **polling**, not webhooks, so you do not need a public URL.

If the service sleeps or restarts, polling resumes and Postgres keeps your data.

## Environment

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | From BotFather |
| `DATABASE_URL` | no | Defaults to `sqlite:///data.db`. Railway Postgres uses `postgresql://...` |
| `ALLOWED_USER_IDS` | strongly yes in production | Comma-separated Telegram IDs. Empty allows anyone |
| `TZ` | no | Default `Asia/Phnom_Penh` |
