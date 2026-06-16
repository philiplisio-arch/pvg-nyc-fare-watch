# PVG → NYC Business-Class Fare Watcher

A self-running, **publicly hosted** webpage that checks business-class fares from
**Shanghai (PVG) → New York (JFK/EWR)**, depart **2026-12-19**, return **2027-01-01**,
**every hour**. It lists all the best options (cheapest first), and **emails an alert**
to `jeannekang@hotmail.com` whenever a business-class fare drops **under $6,500**.

It runs entirely on **GitHub Actions + GitHub Pages** — no server to maintain, and it
keeps running even when your computer is off.

---

## What it searches (multi-source)

The watcher merges and de-duplicates results from every source it has credentials for,
then reports the single lowest fare across all of them:

| Source | What it adds | Required? |
|---|---|---|
| **Google Flights** (via SerpApi) | Market-wide aggregation of **airline-direct prices + OTAs**, nonstop & connecting (incl. United, Air China, Hainan, and routings via Seoul/Tokyo) | **Yes** |
| **Amadeus** Flight Offers | Airline **published GDS fares** | Optional |
| **Kiwi / Tequila** | **Virtual interlining / split-tickets** with real booking deep-links | Optional |

It also computes a **split-ticket** price by summing the cheapest one-way legs in each
direction (which often beats the round-trip fare and naturally routes via Seoul/Tokyo).

> **On "checking airline websites directly":** scraping individual carrier sites
> (united.com, airchina.com, hainanairlines.com) on an unattended hourly schedule is
> unreliable — those sites use bot protection, JS-rendered fares, and login walls that
> break headless jobs. The robust equivalent is what's used here: **Google Flights and
> Amadeus surface the airlines' own published fares**, and every result links straight to
> the airline/OTA booking page. (If you ever want true per-site scraping, that needs a
> browser session running while your computer is on — a separate, non-public setup.)

---

## One-time setup (~10 minutes)

### 1. Create the repository
Create a **new GitHub repo** (e.g. `pvg-nyc-fare-watch`) and add these files at the root:

```
check_flights.py
requirements.txt
index.html
.github/workflows/hourly.yml      <- this is the file named "hourly.yml"
```

(Put `hourly.yml` inside a `.github/workflows/` folder.)

### 2. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `SERPAPI_KEY` | **Required.** Your SerpApi key (you already have SerpApi access) — find it at serpapi.com → *Your Account → Api Key*. |
| `GMAIL_USER` | The Gmail address the alert is sent **from** (e.g. your Gmail). |
| `GMAIL_APP_PASSWORD` | A Gmail **App Password** (see below). Not your normal password. |
| `AMADEUS_CLIENT_ID` | *(optional)* Amadeus self-service API key. |
| `AMADEUS_CLIENT_SECRET` | *(optional)* Amadeus self-service secret. |
| `KIWI_API_KEY` | *(optional)* Kiwi/Tequila API key. |

Optional **Variables** (Settings → Variables tab) to tweak without editing code:
`ALERT_TO` (default `jeannekang@hotmail.com`), `THRESHOLD_USD` (default `6500`).

**Getting a Gmail App Password:** Google Account → **Security** → enable **2-Step
Verification** → **App passwords** → create one for "Mail". Paste the 16-character code
as `GMAIL_APP_PASSWORD`. (Gmail SMTP can send to any recipient; free SendGrid/Resend
cannot without a verified domain — that's why this uses Gmail.)

### 3. Turn on GitHub Pages
Repo → **Settings → Pages** → **Source: Deploy from a branch** → Branch: **main**,
folder **/ (root)** → Save. Your public URL will be:

```
https://<your-username>.github.io/<repo-name>/
```

### 4. Run it once
Repo → **Actions** tab → enable workflows if prompted → select **“Hourly business-class
fare check”** → **Run workflow**. After ~1 minute it regenerates `index.html` and (if a
sub-$6,500 fare exists) emails the alert. After that it runs automatically every hour.

---

## How alerts work
- Emails when a fare is **≤ $6,500**.
- Won't spam: it re-alerts only on a **new lower price** or after **24h** (`REALERT_HOURS`).
- The email includes airline(s), dates, price, routing, and a booking link.

## Customizing
All settings are environment variables at the top of `check_flights.py`
(`ORIGIN`, `DEST`, `HUBS`, `OUTBOUND_DATE`, `RETURN_DATE`, `THRESHOLD_USD`, …).
You can override any of them as repo Variables/Secrets without touching code.

## API usage note
Each hourly run makes ~3 SerpApi calls (round-trip + two one-ways) = ~2,200/month.
Make sure your SerpApi plan covers that, or change the cron in `hourly.yml`
(e.g. `0 */2 * * *` for every 2 hours).
