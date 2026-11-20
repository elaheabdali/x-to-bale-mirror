# X (Twitter) Home Timeline to Bale Messenger Mirror Bot

An autonomous, 24/7 Python service that monitors the authenticated X (Twitter) **Home Timeline ("Following" feed)** for `@dontcallmeela` and forwards every new post—including text, expanded URLs, retweets, quote tweets, photos, videos, animated GIFs, and NSFW/+18 media—directly to a **Bale (بله)** chat or channel via the [Bale Bot API](https://docs.bale.ai/).

## Project Structure

```text
.
├── src/
│   ├── __init__.py
│   ├── main.py          # FastAPI /health server + async polling worker
│   ├── x_client.py      # twikit cookie auth, timeline fetcher & media downloader
│   ├── bale_client.py   # Async httpx Bale Bot API client with retry & fallbacks
│   ├── storage.py       # Dual PostgreSQL (asyncpg) + SQLite (aiosqlite) store
│   └── formatter.py     # RTL/LTR-safe Bale Markdown formatter & caption splitter
├── tests/
│   └── test_bot.py      # Unit tests for formatter, storage, and normalization
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Features

- **No Paid X API Required:** Uses cookie-based authentication (`X_AUTH_TOKEN`, `X_CT0`) via [`twikit`](https://twikit.readthedocs.io/en/latest/twikit.html) (`get_latest_timeline`).
- **Zero Content Filtering:** Forwards all posts without moderation or NSFW/+18 filtering.
- **Full Media Support:**
  - Single photos (`sendPhoto`) and multi-photo albums (`sendMediaGroup` with sequential `sendPhoto` fallback).
  - Videos (`sendVideo`) and X looping GIFs (`sendAnimation` with `sendVideo` fallback) downloaded to `/tmp` and deleted immediately after upload.
  - Automatic fallback to text + direct media link if a video exceeds `MAX_VIDEO_SIZE_MB` or fails to upload.
  - Automatic caption splitting when formatted text exceeds Bale's 1,024-character caption limit.
- **Cold-Start Flood Protection:** On an empty database, seeds only the latest `COLD_START_SEED_COUNT` (default `8`) tweet IDs without flooding Bale, then forwards every new post in chronological order.
- **24/7 Cloud Ready:** Exposes `GET /health` via FastAPI for Render, Railway, Koyeb, and UptimeRobot keep-alive pings, with randomized polling jitter (`180s ± 15s`), exponential backoff on `429`/network errors, and Bale admin alerts on cookie expiry (`401`/`403`).

## Setup Instructions

### 1. Get X (Twitter) Cookies for `@dontcallmeela`
1. Log in to [x.com](https://x.com) as `@dontcallmeela` in a desktop browser.
2. Open **DevTools** (`F12`) → **Application** → **Cookies** → `https://x.com`.
3. Copy the values of `auth_token` (`X_AUTH_TOKEN`) and `ct0` (`X_CT0`).

### 2. Create a Bale Bot
1. Message `@botfather` in Bale Messenger to create a new bot and obtain `BALE_BOT_TOKEN` ([Bale Bot Docs](https://docs.bale.ai/)).
2. Add the bot as an administrator to your target channel/group (or start a private chat) and set `BALE_CHAT_ID`.

### 3. Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your tokens, cookies, and optional DATABASE_URL

uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Check health:

```bash
curl http://localhost:8000/health
```

### 4. Run with Docker

```bash
docker build -t x-bale-mirror .
docker run -d --name x-bale-mirror --env-file .env -p 8000:8000 x-bale-mirror
```

## 24/7 Free Cloud Deployment (Render / Railway / Koyeb)

1. Create a free PostgreSQL database on **Neon** or **Supabase** and copy its connection string to `DATABASE_URL`.
2. Deploy this repository as a **Web Service** (Docker runtime) on **Render**, **Railway**, or **Koyeb**.
3. Set all environment variables from `.env.example` in your platform's dashboard.
4. Configure **UptimeRobot** (free tier) to send an HTTP `GET` request every 5 minutes to `https://<your-app-domain>/health` so the container stays awake 24/7.
