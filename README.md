# 🎵 Mumbai Music Event Scraper

A fully automated, multi-platform event scraper that monitors live music and nightlife events across Mumbai — and delivers a curated email digest whenever something new drops.

Built with Python, Playwright, and GitHub Actions. Zero infrastructure. Runs on a schedule, costs nothing to host.

---

## How It Works

The scraper runs on a GitHub Actions cron job. On each trigger, it:

1. Launches a headless Chromium browser via Playwright
2. Scrapes four platforms **concurrently** using `asyncio.gather()`
3. Diffs results against a snapshot of previously seen events
4. Sends an HTML email digest listing only the **new** events — grouped by source, with title, venue, date, and price
5. Updates the snapshot for the next run

---

## Platforms Covered

| Platform | Category |
|---|---|
| [BookMyShow](https://in.bookmyshow.com/explore/events-mumbai?categories=music-shows) | Music Shows |
| [Skillboxes](https://www.skillboxes.com/events-mumbai) | All Events |
| [District](https://www.district.in/events/music-in-mumbai-book-tickets) | Music |
| [SortMyScene](https://sortmyscene.com/events?tab=events&city=Mumbai) | Nightlife & Music |

---

## Tech Stack

- **Python 3.11+**
- **Playwright** — headless Chromium for JS-rendered SPAs
- **asyncio** — concurrent scraping across all platforms in a single browser instance
- **smtplib** — HTML email via Gmail SMTP
- **GitHub Actions** — scheduled execution, secret management, zero-cost hosting

---

## Project Structure

```
bms-event-scraper/
├── scraper.py              # All scraping, diffing, and email logic
├── known_events.json       # Snapshot of seen event titles (auto-updated)
└── .github/
    └── workflows/
        └── scrape.yml      # GitHub Actions workflow
```

---

## Setup

### 1. Fork or clone the repo

```bash
git clone https://github.com/prashanta-dev7/bms-event-scraper.git
cd bms-event-scraper
```

### 2. Install dependencies

```bash
pip install playwright
playwright install chromium
```

### 3. Configure environment variables

The scraper reads credentials from environment variables. For local runs:

```bash
export SENDER_EMAIL="you@gmail.com"
export SENDER_PASSWORD="your-gmail-app-password"
export RECIPIENT_EMAIL="you@gmail.com"
```

> **Note:** `SENDER_PASSWORD` must be a [Gmail App Password](https://myaccount.google.com/apppasswords), not your regular account password. Requires 2FA to be enabled on the sending account.

For GitHub Actions, add these as **repository secrets** under `Settings → Secrets and variables → Actions`.

### 4. Run locally

```bash
python scraper.py
```

To test without sending email, comment out `send_email(new_events)` in `main()` and print results instead.

### 5. Debug mode

Set `DEBUG=1` to capture and email screenshots if any scraper fails — useful for diagnosing bot detection or DOM changes:

```bash
DEBUG=1 python scraper.py
```

---

## GitHub Actions Workflow

The workflow triggers on a cron schedule and supports manual runs via `workflow_dispatch`.

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "0 6 * * *"   # runs daily at 6 AM UTC
```

Secrets (`SENDER_EMAIL`, `SENDER_PASSWORD`, `RECIPIENT_EMAIL`) are passed as environment variables at runtime — nothing sensitive is stored in the codebase.

---

## Engineering Notes

**Why Playwright over requests/BeautifulSoup?**
All four platforms are React or JS-heavy SPAs. Static HTTP fetches return near-empty HTML. Playwright renders the full DOM, handles hydration delays, and supports scroll-based lazy loading — essential for getting real event data.

**Concurrent scraping**
All four scrapers run simultaneously via `asyncio.gather()`, sharing a single Chromium instance with isolated browser contexts. This keeps total runtime low despite four separate page loads.

**Resilient DOM parsing**
Each scraper uses a waterfall of CSS selectors, falling back to anchor-href filtering if no class-based selector matches. A DOM-walk (up to 4 ancestor levels) finds the card container holding venue and price siblings — making the parser tolerant of varying component depths.

**Platform-specific parsers**
Card line order varies by platform. BMS and Skillboxes use a generic `parse_card_lines()` helper. District and SortMyScene use `parse_district_card()`, which handles date-first card layouts and multi-line price strings like `₹1800\nonwards`.

**Debug screenshots**
When `DEBUG=1`, any scraper that fails captures a full-page screenshot, base64-encodes it, and attaches it inline to a separate debug email — making remote diagnosis possible without access to the runner.

---

## Email Output

New events are grouped by platform with brand colours, linked titles, venue, date (where available), and price. Example subject line:

```
🎵 12 New Event(s) in Mumbai! (BookMyShow, District, SortMyScene)
```

---

## Potential Improvements

- Persist `known_events.json` via git commit or external store (current GitHub Actions runner is ephemeral)
- Deduplicate by URL slug instead of title string
- Add category/genre filtering
- Extend to other cities
- Slack or WhatsApp notification channel alongside email

---

## License

MIT
