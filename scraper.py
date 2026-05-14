import asyncio
import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright, Browser

# Read from environment variables (set as GitHub Secrets)
SENDER_EMAIL    = os.environ["SENDER_EMAIL"]
SENDER_PASSWORD = os.environ["SENDER_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

BMS_URL        = "https://in.bookmyshow.com/explore/events-mumbai?categories=music-shows"
SKILLBOXES_URL = "https://www.skillboxes.com/events-mumbai"
DISTRICT_URL   = "https://www.district.in/events/music-in-mumbai-book-tickets"
SNAPSHOT_FILE  = "known_events.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── BookMyShow ────────────────────────────────────────────────────────────────

async def scrape_bms(browser: Browser) -> list[dict]:
    events = []
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    print("🌐 [BMS] Navigating...")
    try:
        await page.goto(BMS_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('a[href*="/events/"]', timeout=30000)

        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(1200)

        card_els = await page.query_selector_all('a[href*="/events/"]')
        print(f"   ↳ {len(card_els)} cards found")

        seen_titles: set[str] = set()
        for card in card_els:
            try:
                inner_text = await card.inner_text()
                lines = [l.strip() for l in inner_text.strip().split('\n') if l.strip()]
                if not lines:
                    continue
                title = lines[0]
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                venue = lines[1] if len(lines) > 1 else "Venue TBA"
                price = lines[3] if len(lines) > 3 else (lines[-1] if len(lines) > 1 else "")
                href  = await card.get_attribute("href") or ""
                url   = href if href.startswith("http") else f"https://in.bookmyshow.com{href}"
                events.append({
                    "title": title, "venue": venue,
                    "price": price, "url": url,
                    "source": "BookMyShow"
                })
            except Exception:
                continue
    except Exception as e:
        print(f"   ⚠️  [BMS] Error: {e}")
    finally:
        await context.close()

    print(f"✅ [BMS] {len(events)} unique events")
    return events


# ── Skillboxes ────────────────────────────────────────────────────────────────

async def scrape_skillboxes(browser: Browser) -> list[dict]:
    events = []
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    print("🌐 [Skillboxes] Navigating...")
    try:
        await page.goto(SKILLBOXES_URL, wait_until="networkidle", timeout=60000)

        # Scroll to trigger lazy loading
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(1200)

        # Skillboxes event cards are typically anchor tags linking to /e/ or /event/ paths
        # Try multiple selector patterns to be resilient
        card_els = (
            await page.query_selector_all('a[href*="/e/"]') or
            await page.query_selector_all('a[href*="/event/"]') or
            await page.query_selector_all('[class*="event-card"] a') or
            await page.query_selector_all('[class*="EventCard"] a') or
            await page.query_selector_all('[class*="card"] a[href]')
        )
        print(f"   ↳ {len(card_els)} cards found")

        seen_titles: set[str] = set()
        for card in card_els:
            try:
                inner_text = await card.inner_text()
                lines = [l.strip() for l in inner_text.strip().split('\n') if l.strip()]
                if not lines:
                    continue
                title = lines[0]
                if not title or title in seen_titles or len(title) < 3:
                    continue
                seen_titles.add(title)
                venue = next((l for l in lines[1:] if l and not l.startswith("₹")), "Venue TBA")
                price = next((l for l in lines if "₹" in l or l.lower().startswith("free")), "")
                href  = await card.get_attribute("href") or ""
                url   = href if href.startswith("http") else f"https://www.skillboxes.com{href}"
                events.append({
                    "title": title, "venue": venue,
                    "price": price, "url": url,
                    "source": "Skillboxes"
                })
            except Exception:
                continue

        # Fallback: if selectors above returned nothing, try scraping visible text blocks
        if not events:
            print("   ↳ Selector fallback: trying broader card search...")
            card_els = await page.query_selector_all('a[href]')
            seen_titles = set()
            for card in card_els:
                try:
                    href = await card.get_attribute("href") or ""
                    # Skip nav/footer/social links
                    if not href or href in ("#", "/") or "skillboxes.com" not in href and not href.startswith("/e"):
                        continue
                    inner_text = await card.inner_text()
                    lines = [l.strip() for l in inner_text.strip().split('\n') if l.strip()]
                    if not lines or len(lines[0]) < 5:
                        continue
                    title = lines[0]
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    url = href if href.startswith("http") else f"https://www.skillboxes.com{href}"
                    events.append({
                        "title": title, "venue": lines[1] if len(lines) > 1 else "Venue TBA",
                        "price": next((l for l in lines if "₹" in l or "free" in l.lower()), ""),
                        "url": url, "source": "Skillboxes"
                    })
                except Exception:
                    continue

    except Exception as e:
        print(f"   ⚠️  [Skillboxes] Error: {e}")
    finally:
        await context.close()

    print(f"✅ [Skillboxes] {len(events)} unique events")
    return events


# ── District ──────────────────────────────────────────────────────────────────

async def scrape_district(browser: Browser) -> list[dict]:
    events = []
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    print("🌐 [District] Navigating...")
    try:
        await page.goto(DISTRICT_URL, wait_until="networkidle", timeout=60000)

        # Wait for event content to render
        try:
            await page.wait_for_selector('a[href*="/events/"]', timeout=20000)
        except Exception:
            # District may use a different structure; continue anyway
            pass

        # Scroll to load more events
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(1200)

        # District event links go to /events/<slug>
        card_els = (
            await page.query_selector_all('a[href*="/events/"]') or
            await page.query_selector_all('[class*="event"] a') or
            await page.query_selector_all('[class*="card"] a[href]')
        )
        print(f"   ↳ {len(card_els)} cards found")

        seen_titles: set[str] = set()
        for card in card_els:
            try:
                inner_text = await card.inner_text()
                lines = [l.strip() for l in inner_text.strip().split('\n') if l.strip()]
                if not lines:
                    continue
                title = lines[0]
                # Skip generic navigation links
                if not title or title in seen_titles or len(title) < 4:
                    continue
                if title.lower() in {"events", "music", "mumbai", "district", "home"}:
                    continue
                seen_titles.add(title)
                venue = next((l for l in lines[1:] if l and not l.startswith("₹") and len(l) > 3), "Venue TBA")
                price = next((l for l in lines if "₹" in l or l.lower().startswith("free")), "")
                href  = await card.get_attribute("href") or ""
                url   = href if href.startswith("http") else f"https://www.district.in{href}"
                # Skip the category page link itself
                if url == DISTRICT_URL:
                    continue
                events.append({
                    "title": title, "venue": venue,
                    "price": price, "url": url,
                    "source": "District"
                })
            except Exception:
                continue

    except Exception as e:
        print(f"   ⚠️  [District] Error: {e}")
    finally:
        await context.close()

    print(f"✅ [District] {len(events)} unique events")
    return events


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def scrape_all_events() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        # Run all three scrapers concurrently
        bms_events, skillboxes_events, district_events = await asyncio.gather(
            scrape_bms(browser),
            scrape_skillboxes(browser),
            scrape_district(browser),
        )
        await browser.close()

    all_events = bms_events + skillboxes_events + district_events
    print(f"\n📊 Total scraped: {len(all_events)} events across 3 sources")
    return all_events


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def load_known_events() -> set[str]:
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return set(json.load(f))
    return set()


def save_known_events(titles: set[str]) -> None:
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(list(titles), f, indent=2)


# ── Email ─────────────────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "BookMyShow": "#e2163b",
    "Skillboxes": "#6c3cff",
    "District":   "#ff6b00",
}

def build_source_section(source: str, events: list[dict]) -> str:
    color = SOURCE_COLORS.get(source, "#333")
    rows = ""
    for ev in events:
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;">
            <a href="{ev['url']}" style="font-weight:600;color:{color};text-decoration:none;">{ev['title']}</a>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#555;">{ev['venue']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#27ae60;">{ev.get('price', '')}</td>
        </tr>"""

    return f"""
    <h3 style="color:{color};margin-top:28px;border-left:4px solid {color};padding-left:10px;">
      {source} &nbsp;<span style="font-size:13px;font-weight:normal;color:#888;">({len(events)} new)</span>
    </h3>
    <table width="100%" style="border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f5f5f5;">
          <th style="padding:10px 12px;text-align:left;">Event</th>
          <th style="padding:10px 12px;text-align:left;">Venue</th>
          <th style="padding:10px 12px;text-align:left;">Price</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def send_email(new_events: list[dict]) -> None:
    # Group by source
    by_source: dict[str, list[dict]] = {}
    for ev in new_events:
        by_source.setdefault(ev["source"], []).append(ev)

    sections = "".join(
        build_source_section(src, evs)
        for src, evs in by_source.items()
    )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px;">
      <h2 style="color:#333;">🎵 New Music Events in Mumbai</h2>
      <p style="color:#555;">
        Found <strong>{len(new_events)}</strong> new event(s) across
        <strong>{len(by_source)}</strong> source(s) as of
        {datetime.now().strftime('%d %b %Y, %I:%M %p')} UTC.
      </p>
      {sections}
      <p style="margin-top:30px;font-size:12px;color:#aaa;">
        Auto-generated by your Mumbai Music Scraper 🤖 &nbsp;|&nbsp;
        Sources: BookMyShow · Skillboxes · District
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎵 {len(new_events)} New Music Event(s) in Mumbai! ({', '.join(by_source)})"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"📧 Email sent → {RECIPIENT_EMAIL}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    known   = load_known_events()
    scraped = await scrape_all_events()

    if not scraped:
        print("⚠️ No events scraped from any source.")
        return

    new_events = [ev for ev in scraped if ev["title"] not in known]

    if new_events:
        print(f"\n🆕 {len(new_events)} new event(s) found!")
        send_email(new_events)
    else:
        print("✅ No new events since last check.")

    save_known_events(known | {ev["title"] for ev in scraped})
    print("💾 Snapshot updated")


asyncio.run(main())
