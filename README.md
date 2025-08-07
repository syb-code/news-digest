# News Digest Starter (GitHub Pages + GitHub Actions)

A $0/month personal news dashboard that aggregates **newsletters (via RSS)**, **YouTube channels**, and **websites** into a single page with **Highlights**, **Deeper Dives**, **filters**, and **search**. It also sends a **daily email digest** at **7:30 AM PT** from your collector Gmail (e.g., `mikayell9@gmail.com`) to your main inbox (e.g., `mgome028@gmail.com`).

## What you get
- Static website on **GitHub Pages** (`/docs` folder) with:
  - Highlights vs Deeper Dives
  - Filters by **source** and **theme**
  - **Search** powered by Lunr.js
  - Embedded timelines for selected X/Twitter accounts (web only)
- Scheduled aggregator (hourly) to fetch/normalize feeds
- Scheduled daily email (7:30 AM PT) via **Gmail API** (no keys in code; uses repo secrets)

## Quick Start (10–15 minutes)
1) **Create a new GitHub repo** (public). Upload this project.
2) **Enable Pages**: Settings → Pages → *Deploy from a branch* → Branch: **main**, Folder: **/docs** → Save.
3) **Install dependencies for Actions** (automated): handled by the workflows.
4) **Add your feeds** in `feeds.yml` and themes in `themes.yml` (see below).
5) **Set Secrets** (Settings → Secrets and variables → Actions → New repository secret):
   - `GMAIL_CLIENT_ID` — From Google Cloud OAuth client (Desktop app)
   - `GMAIL_CLIENT_SECRET` — From Google Cloud OAuth client
   - `GMAIL_REFRESH_TOKEN` — Refresh token for the collector Gmail (`mikayell9@gmail.com`)
   - `SENDER_EMAIL` — `mikayell9@gmail.com`
   - `RECIPIENT_EMAIL` — `mgome028@gmail.com`
6) The site will auto-build hourly. The **daily email** goes out at **14:30 UTC** (7:30 AM PT during Daylight Savings).

> **DST note:** GitHub Actions uses **UTC** for schedules. The provided email workflow runs at **14:30 UTC** (7:30 AM during **PDT**). During **PST**, it will hit at **06:30** local. If you want a strict 7:30 local send, adjust the cron twice a year or use an external scheduler.

---

## Feeds Configuration
Edit **`feeds.yml`**:

```yaml
youtube:
  # You can provide either channel_id or handle (script resolves handle → channel_id)
  - handle: clearvaluetax9382
  - handle: AIDailyBrief
  - handle: Monetary-Matters

rss:
  # Example: Kill the Newsletter! outputs an RSS URL for each newsletter address
  - label: Example Newsletter
    feed_url: https://example.com/rss.xml

websites:
  - label: Example Site
    feed_url: https://example.com/feed

twitter_embeds:
  # Web dashboard embeds only (email does not include tweets by default)
  - handle: Polymarket
  - handle: zerohedge
  - handle: danielrpopper
```

Themes in **`themes.yml`** (keywords are case-insensitive; simple OR-match):

```yaml
themes:
  Macro/Fed:
    - fed
    - fomc
    - interest rates
    - powell
  Crypto:
    - bitcoin
    - btc
    - crypto
    - coinbase
    - ethereum
    - solana
  AI/ML:
    - ai
    - machine learning
    - generative
    - openai
    - nvidia
  Housing (CA/SD):
    - housing
    - san diego
    - california
    - mortgage
  Tech/Earnings:
    - earnings
    - guidance
    - revenue
    - net income
    - Shopify
    - COIN
```

---

## Gmail API Setup (one-time)
1. Go to **Google Cloud Console** → create a project.
2. **Enable** the **Gmail API**.
3. **OAuth consent screen**: External (Testing is fine), add your Gmail as a test user.
4. **Credentials** → **Create Credentials** → **OAuth client ID** → **Desktop app**.
5. Note your **Client ID** and **Client Secret**.
6. Generate a **Refresh Token** for the collector Gmail (`mikayell9@gmail.com`):
   - Use the Google OAuth 2.0 Playground: https://developers.google.com/oauthplayground
   - Select scope: `https://mail.google.com/`
   - Authorize and exchange for tokens
   - Copy the **Refresh Token** and put it in the repo secret `GMAIL_REFRESH_TOKEN`
7. Set the other secrets: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `SENDER_EMAIL`, `RECIPIENT_EMAIL`.

> The workflows will exchange the refresh token for an **access token** on each run and send via the Gmail API. No credentials are committed to the repo.

---

## How it works
- **Aggregator**: `scripts/fetch_and_build.py`
  - Reads `feeds.yml` & `themes.yml`
  - Resolves YouTube handles to channel IDs (no API key) by parsing the channel page
  - Pulls all feeds, normalizes items, assigns **source** and **theme(s)**
  - Buckets into **Highlights** (short) vs **Deeper Dives** (long) using simple length heuristics
  - Writes `docs/data/items.json`
- **Dashboard**: `docs/index.html` + `docs/assets/js/app.js`
  - Loads `items.json`, renders lists, adds filters and search
  - Embeds X/Twitter timelines listed under `twitter_embeds`
- **Email**: `scripts/send_email.py`
  - Selects last 24h items, builds HTML, and sends via Gmail API

---

## Local Development
- Install Python 3.10+
- `pip install -r requirements.txt`
- `python scripts/fetch_and_build.py`
- Open `docs/index.html` locally

---

## Adjusting Schedules
- **Hourly aggregator**: `.github/workflows/aggregate.yml` → `schedule.cron`
- **Daily email**: `.github/workflows/daily_email.yml` → `schedule.cron` (UTC)

---

## Notes
- This starter avoids paid APIs. For robust **X/Twitter ingestion into email**, consider a paid RSS service or X API.
- For Spotify or other sources later, you can add modules to `scripts/fetch_and_build.py`.
