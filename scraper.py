import asyncio
import base64
import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright, Browser, Page

# ── Config ────────────────────────────────────────────────────────────────────

SENDER_EMAIL    = os.environ["SENDER_EMAIL"]
SENDER_PASSWORD = os.environ["SENDER_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

# Set DEBUG=1 in env to attach screenshots to the email on scrape failure
DEBUG = os.environ.get("DEBUG", "0") == "1"

BMS_URL        = "https://in.bookmyshow.com/explore/events-mumbai?categories=music-shows"
SKILLBOXES_URL = "https://www.skillboxes.com/events-mumbai"
DISTRICT_URL   = "https://www.district.in/events/music-in-mumbai-book-tickets"
SORTMYSCENE_URL = "https://sortmyscene.com/events?tab=events&city=Mumbai"
SNAPSHOT_FILE  = "known_events.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Holds base64 debug screenshots if any scraper fails
_debug_screenshots: list[dict] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

async def slow_scroll(page: Page, rounds: int = 6, delay_ms: int = 1200) -> None:
    for _ in range(rounds):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(delay_ms)


async def capture_debug(page: Page, label: str) -> None:
    if not DEBUG:
        return
    try:
        png = await page.screenshot(full_page=False)
        _debug_screenshots.append({
            "label": label,
            "data": base64.b64encode(png).decode()
        })
        print(f"   📸 Debug screenshot captured: {label}")
    except Exception:
        pass


def parse_card_lines(lines: list[str]) -> tuple[str, str, str]:
    """Return (title, venue, price) from a card's text lines."""
    title = lines[0] if lines else ""
    venue = next(
        (l for l in lines[1:] if l and "₹" not in l and not l.lower().startswith("free") and len(l) > 3),
        "Venue TBA"
    )
    price = next(
        (l for l in lines if "₹" in l or l.lower().startswith("free")),
        ""
    )
    return title, venue, price


# ── BookMyShow ────────────────────────────────────────────────────────────────

async def scrape_bms(browser: Browser) -> list[dict]:
    events: list[dict] = []
    ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    print("🌐 [BMS] Navigating...")
    try:
        await page.goto(BMS_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('a[href*="/events/"]', timeout=30000)
        await slow_scroll(page)

        seen: set[str] = set()
        for card in await page.query_selector_all('a[href*="/events/"]'):
            try:
                raw = await card.inner_text()
                lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
                if not lines:
                    continue
                title, venue, price = parse_card_lines(lines)
                if not title or title in seen:
                    continue
                seen.add(title)
                href = await card.get_attribute("href") or ""
                url  = href if href.startswith("http") else f"https://in.bookmyshow.com{href}"
                events.append({"title": title, "venue": venue, "price": price, "url": url, "source": "BookMyShow"})
            except Exception:
                continue
    except Exception as e:
        print(f"   ⚠️  [BMS] Error: {e}")
        await capture_debug(page, "BMS-error")
    finally:
        await ctx.close()
    print(f"✅ [BMS] {len(events)} events")
    return events


# ── Skillboxes ────────────────────────────────────────────────────────────────

async def scrape_skillboxes(browser: Browser) -> list[dict]:
    events: list[dict] = []
    ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    print("🌐 [Skillboxes] Navigating...")
    try:
        await page.goto(SKILLBOXES_URL, wait_until="domcontentloaded", timeout=60000)

        # Give JS extra time to hydrate
        await page.wait_for_timeout(5000)
        await slow_scroll(page, rounds=6, delay_ms=1500)

        html_len = len(await page.content())
        print(f"   ↳ Page content length after hydration: {html_len} chars")

        # Strategy 1: known event URL patterns
        selectors_to_try = [
            'a[href*="/e/"]',
            'a[href*="/event/"]',
            '[class*="event"] a[href]',
            '[class*="Event"] a[href]',
            '[class*="card"] a[href]',
            '[class*="Card"] a[href]',
            '[class*="listing"] a[href]',
            '[class*="tile"] a[href]',
        ]

        card_els = []
        for sel in selectors_to_try:
            found = await page.query_selector_all(sel)
            if found:
                print(f"   ↳ Selector '{sel}' → {len(found)} elements")
                card_els = found
                break

        # Strategy 2: all anchors filtered by path pattern
        if not card_els:
            print("   ↳ No selector matched; falling back to all anchors")
            all_anchors = await page.query_selector_all('a[href]')
            print(f"   ↳ Total anchors on page: {len(all_anchors)}")
            for a in all_anchors:
                href = (await a.get_attribute("href") or "").lower()
                if any(p in href for p in ["/e/", "/event/", "skillboxes.com/e", "skillboxes.com/event"]):
                    card_els.append(a)
            print(f"   ↳ Filtered event anchors: {len(card_els)}")

        if not card_els:
            print("   ⚠️  [Skillboxes] No event links found. Dumping HTML for diagnosis:")
            content = await page.content()
            print(content[:2000])
            await capture_debug(page, "Skillboxes-no-cards")

        seen: set[str] = set()
        for card in card_els:
            try:
                href = await card.get_attribute("href") or ""
                # The <a> tag may only contain the title.
                # Walk up the DOM to find the card container that holds venue/price siblings.
                # Try up to 4 ancestor levels to find a node with more content.
                title_text = (await card.inner_text()).strip()
                lines = [l.strip() for l in title_text.splitlines() if l.strip()]
                title = lines[0] if lines else ""
                if not title or len(title) < 3 or title in seen:
                    continue

                # Walk up DOM looking for a parent that has venue/price text
                container_text = title_text
                for level in range(1, 5):
                    ancestor = await card.evaluate_handle(
                        f"el => {{ let n = el; for(let i=0;i<{level};i++) n = n.parentElement; return n; }}"
                    )
                    if not ancestor:
                        break
                    parent_text = await (await ancestor.get_property("innerText")).json_value()
                    parent_lines = [l.strip() for l in (parent_text or "").splitlines() if l.strip()]
                    # Accept this level if it adds venue/price without bloating too much
                    if len(parent_lines) >= 2 and len(parent_lines) <= 12:
                        container_text = parent_text
                        break

                all_lines = [l.strip() for l in container_text.splitlines() if l.strip()]

                # Skillboxes card text order (confirmed from screenshot):
                #   line[0] = event title
                #   remaining lines may contain venue, date, price in varying order
                venue = next(
                    (l for l in all_lines[1:] if l and "₹" not in l
                     and not l.lower().startswith("free")
                     and not any(d in l.lower() for d in ["onwards","seats","left","sold"])
                     and len(l) > 3),
                    "Venue TBA"
                )
                price = next(
                    (l for l in all_lines if "₹" in l or l.lower().startswith("free")),
                    ""
                )
                # Handle "₹500\nonwards" split across lines
                if price:
                    idx = all_lines.index(price)
                    if idx + 1 < len(all_lines) and all_lines[idx+1].lower() == "onwards":
                        price = f"{price} onwards"

                seen.add(title)
                url = href if href.startswith("http") else f"https://www.skillboxes.com{href}"
                events.append({"title": title, "venue": venue, "price": price, "url": url, "source": "Skillboxes"})
            except Exception:
                continue

    except Exception as e:
        print(f"   ⚠️  [Skillboxes] Error: {e}")
        await capture_debug(page, "Skillboxes-error")
    finally:
        await ctx.close()
    print(f"✅ [Skillboxes] {len(events)} events")
    return events


# ── District ──────────────────────────────────────────────────────────────────

async def scrape_district(browser: Browser) -> list[dict]:
    events: list[dict] = []
    ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    print("🌐 [District] Navigating...")
    try:
        await page.goto(DISTRICT_URL, wait_until="domcontentloaded", timeout=60000)

        # District is a heavy React SPA — needs extra hydration time
        await page.wait_for_timeout(6000)
        await slow_scroll(page, rounds=6, delay_ms=1500)

        html_len = len(await page.content())
        print(f"   ↳ Page content length after hydration: {html_len} chars")

        # District event URLs end with /event
        selectors_to_try = [
            'a[href$="/event"]',
            'a[href*="/event"]',
            '[class*="event-card"] a',
            '[class*="EventCard"] a',
            '[class*="event_card"] a',
            '[class*="listing"] a[href]',
            '[class*="card"] a[href]',
        ]

        card_els = []
        for sel in selectors_to_try:
            found = await page.query_selector_all(sel)
            if found:
                print(f"   ↳ Selector '{sel}' → {len(found)} elements")
                card_els = found
                break

        # Fallback: all anchors with /event in path
        if not card_els:
            print("   ↳ No selector matched; falling back to all anchors")
            all_anchors = await page.query_selector_all('a[href]')
            print(f"   ↳ Total anchors on page: {len(all_anchors)}")
            for a in all_anchors:
                href = (await a.get_attribute("href") or "").lower()
                if href.endswith("/event") or "/event?" in href:
                    card_els.append(a)
            print(f"   ↳ Filtered event anchors: {len(card_els)}")

        if not card_els:
            print("   ⚠️  [District] No event links found. Dumping HTML for diagnosis:")
            content = await page.content()
            print(content[:2000])
            await capture_debug(page, "District-no-cards")

        SKIP_TITLES = {
            "events", "music", "mumbai", "district", "home", "search",
            "sign in", "login", "download", "terms", "privacy", "contact"
        }

        # District card text order (confirmed from screenshot):
        #   lines[0] = date/time  e.g. "Sat, 16 May, 8:45 PM"
        #   lines[1] = event name e.g. "Unplugged Night ft. Chitranshu"
        #   lines[2] = venue      e.g. "Candlelight Singalong | Lyla"
        #   lines[-1] = price     e.g. "₹200"  (may be absent)
        def parse_district_card(lines: list[str]) -> tuple[str, str, str, str]:
            # Detect if first line looks like a date (contains a month name or day name)
            DATE_HINTS = {"mon","tue","wed","thu","fri","sat","sun","jan","feb","mar",
                          "apr","may","jun","jul","aug","sep","oct","nov","dec","every"}
            first_lower = lines[0].lower() if lines else ""
            is_date_first = any(h in first_lower for h in DATE_HINTS)

            if is_date_first and len(lines) >= 2:
                date  = lines[0]
                title = lines[1]
                venue = lines[2] if len(lines) > 2 else "Venue TBA"
            else:
                date  = ""
                title = lines[0]
                venue = lines[1] if len(lines) > 1 else "Venue TBA"

            # Price: last line containing ₹ or "free", or join last two lines for "₹1800\nonwards"
            price_lines = [l for l in lines if "₹" in l or l.lower().startswith("free") or l.lower() == "onwards"]
            if len(price_lines) >= 2 and price_lines[-1].lower() == "onwards":
                price = f"{price_lines[-2]} onwards"
            elif price_lines:
                price = price_lines[0]
            else:
                price = ""

            # Clean venue: strip price-like content
            if "₹" in venue or venue.lower().startswith("free"):
                venue = "Venue TBA"

            return title, venue, price, date

        seen: set[str] = set()
        for card in card_els:
            try:
                raw = await card.inner_text()
                lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
                if not lines or len(lines[0]) < 2:
                    continue
                title, venue, price, date = parse_district_card(lines)
                if not title or title in seen or title.lower() in SKIP_TITLES:
                    continue
                href = await card.get_attribute("href") or ""
                url  = href if href.startswith("http") else f"https://www.district.in{href}"
                if url.rstrip("/") == DISTRICT_URL.rstrip("/"):
                    continue
                seen.add(title)
                events.append({"title": title, "venue": venue, "price": price,
                               "date": date, "url": url, "source": "District"})
            except Exception:
                continue

    except Exception as e:
        print(f"   ⚠️  [District] Error: {e}")
        await capture_debug(page, "District-error")
    finally:
        await ctx.close()
    print(f"✅ [District] {len(events)} events")
    return events
    
# ── SortMyScene ───────────────────────────────────────────────────────────────

SORTMYSCENE_URL = "https://sortmyscene.com/events?tab=events&city=Mumbai"

async def scrape_sortmyscene(browser: Browser) -> list[dict]:
    events: list[dict] = []
    ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    print("🌐 [SortMyScene] Navigating...")
    try:
        await page.goto(SORTMYSCENE_URL, wait_until="domcontentloaded", timeout=60000)

        # Heavy React SPA — needs hydration time
        await page.wait_for_timeout(6000)
        await slow_scroll(page, rounds=6, delay_ms=1500)

        html_len = len(await page.content())
        print(f"   ↳ Page content length after hydration: {html_len} chars")

        selectors_to_try = [
            'a[href*="/e/"]',
            'a[href*="/event/"]',
            'a[href*="/events/"]',
            '[class*="event"] a[href]',
            '[class*="Event"] a[href]',
            '[class*="card"] a[href]',
            '[class*="Card"] a[href]',
            '[class*="listing"] a[href]',
            '[class*="tile"] a[href]',
        ]

        card_els = []
        for sel in selectors_to_try:
            found = await page.query_selector_all(sel)
            if found:
                print(f"   ↳ Selector '{sel}' → {len(found)} elements")
                card_els = found
                break

        # Fallback: all anchors filtered by path
        if not card_els:
            print("   ↳ No selector matched; falling back to all anchors")
            all_anchors = await page.query_selector_all('a[href]')
            print(f"   ↳ Total anchors on page: {len(all_anchors)}")
            for a in all_anchors:
                href = (await a.get_attribute("href") or "").lower()
                if any(p in href for p in ["/e/", "/event/", "/events/", "sortmyscene.com/e"]):
                    card_els.append(a)
            print(f"   ↳ Filtered event anchors: {len(card_els)}")

        if not card_els:
            print("   ⚠️  [SortMyScene] No event links found. Dumping HTML snippet:")
            print((await page.content())[:2000])
            await capture_debug(page, "SortMyScene-no-cards")

        SKIP_TITLES = {
            "events", "music", "mumbai", "sortmyscene", "home", "search",
            "sign in", "login", "download", "terms", "privacy", "contact", "nightlife"
        }

        seen: set[str] = set()
        for card in card_els:
            try:
                title_text = (await card.inner_text()).strip()
                lines = [l.strip() for l in title_text.splitlines() if l.strip()]
                title = lines[0] if lines else ""
                if not title or len(title) < 3 or title in seen or title.lower() in SKIP_TITLES:
                    continue

                # Walk up DOM to find card container with venue/price siblings
                container_text = title_text
                for level in range(1, 5):
                    ancestor = await card.evaluate_handle(
                        f"el => {{ let n = el; for(let i=0;i<{level};i++) n = n.parentElement; return n; }}"
                    )
                    if not ancestor:
                        break
                    parent_text = await (await ancestor.get_property("innerText")).json_value()
                    parent_lines = [l.strip() for l in (parent_text or "").splitlines() if l.strip()]
                    if 2 <= len(parent_lines) <= 12:
                        container_text = parent_text
                        break

                all_lines = [l.strip() for l in container_text.splitlines() if l.strip()]
                _, venue, price = parse_card_lines(all_lines)

                seen.add(title)
                href = await card.get_attribute("href") or ""
                url = href if href.startswith("http") else f"https://sortmyscene.com{href}"
                if url.rstrip("/") == SORTMYSCENE_URL.rstrip("/"):
                    continue
                events.append({"title": title, "venue": venue, "price": price, "url": url, "source": "SortMyScene"})
            except Exception:
                continue

    except Exception as e:
        print(f"   ⚠️  [SortMyScene] Error: {e}")
        await capture_debug(page, "SortMyScene-error")
    finally:
        await ctx.close()
    print(f"✅ [SortMyScene] {len(events)} events")
    return events

# ── Orchestrator ──────────────────────────────────────────────────────────────

async def scrape_all_events() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        bms_events, skillboxes_events, district_events, sms_events = await asyncio.gather(
            scrape_bms(browser),
            scrape_skillboxes(browser),
            scrape_district(browser),
            scrape_sortmyscene(browser),
        )
        await browser.close()

    all_events = bms_events + skillboxes_events + district_events + sms_events
    print(f"\n📊 Total: {len(all_events)} events  "
          f"(BMS={len(bms_events)}, Skillboxes={len(skillboxes_events)}, "
          f"District={len(district_events)}, SortMyScene={len(sms_events)})")
    return all_events


# ── Snapshot ──────────────────────────────────────────────────────────────────

def load_known_events() -> set[str]:
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return set(json.load(f))
    return set()


def save_known_events(titles: set[str]) -> None:
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(sorted(titles), f, indent=2)


# ── Email ─────────────────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "BookMyShow": "#e2163b",
    "Skillboxes":  "#6c3cff",
    "District":    "#ff6b00",
    "SortMyScene": "#ff3c6e"
}


def build_source_section(source: str, evs: list[dict]) -> str:
    color = SOURCE_COLORS.get(source, "#333")
    has_date = any(ev.get("date") for ev in evs)

    header_cells = "<th style='padding:10px 12px;text-align:left;'>Event</th>"
    if has_date:
        header_cells += "<th style='padding:10px 12px;text-align:left;'>Date</th>"
    header_cells += "<th style='padding:10px 12px;text-align:left;'>Venue</th>"
    header_cells += "<th style='padding:10px 12px;text-align:left;'>Price</th>"

    rows = ""
    for ev in evs:
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;">
            <a href="{ev['url']}" style="font-weight:600;color:{color};text-decoration:none;">{ev['title']}</a>
          </td>"""
        if has_date:
            rows += f"<td style='padding:10px 12px;border-bottom:1px solid #eee;color:#e07000;white-space:nowrap;'>{ev.get('date','')}</td>"
        rows += f"""
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#555;">{ev['venue']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#27ae60;">{ev.get('price','')}</td>
        </tr>"""

    return f"""
    <h3 style="color:{color};margin-top:28px;border-left:4px solid {color};padding-left:10px;">
      {source} <span style="font-size:13px;font-weight:normal;color:#888;">({len(evs)} new)</span>
    </h3>
    <table width="100%" style="border-collapse:collapse;font-size:14px;">
      <thead><tr style="background:#f5f5f5;">{header_cells}</tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_debug_section() -> str:
    if not _debug_screenshots:
        return ""
    imgs = "".join(
        f'<p><strong>{s["label"]}</strong><br>'
        f'<img src="data:image/png;base64,{s["data"]}" style="max-width:100%;border:1px solid #ddd;"></p>'
        for s in _debug_screenshots
    )
    return f'<h3 style="color:#cc0000;margin-top:28px;">🐛 Debug Screenshots</h3>{imgs}'


def send_email(new_events: list[dict]) -> None:
    by_source: dict[str, list[dict]] = {}
    for ev in new_events:
        by_source.setdefault(ev["source"], []).append(ev)

    sections = "".join(build_source_section(src, evs) for src, evs in by_source.items())

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px;">
      <h2 style="color:#333;">🎵 New Music Events in Mumbai</h2>
      <p style="color:#555;">
        Found <strong>{len(new_events)}</strong> new event(s) across
        <strong>{len(by_source)}</strong> source(s) as of
        {datetime.now().strftime('%d %b %Y, %I:%M %p')} UTC.
      </p>
      {sections}
      {build_debug_section()}
      <p style="margin-top:30px;font-size:12px;color:#aaa;">
        Auto-generated 🤖 &nbsp;|&nbsp; Sources: BookMyShow · Skillboxes · District
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎵 {len(new_events)} New Event(s) in Mumbai! ({', '.join(by_source)})"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"📧 Email sent → {RECIPIENT_EMAIL}")


def send_debug_email() -> None:
    if not _debug_screenshots:
        return
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px;">
      <h2 style="color:#cc0000;">🐛 Scraper Debug Report</h2>
      <p>Scrapers ran on {datetime.now().strftime('%d %b %Y, %I:%M %p')} UTC — screenshots attached below.</p>
      {build_debug_section()}
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🐛 Mumbai Scraper — Debug Screenshots"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"📧 Debug email sent → {RECIPIENT_EMAIL}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    known   = load_known_events()
    scraped = await scrape_all_events()

    if not scraped:
        print("⚠️ No events scraped from any source.")
        if DEBUG:
            send_debug_email()
        return

    new_events = [ev for ev in scraped if ev["title"] not in known]

    if new_events:
        print(f"\n🆕 {len(new_events)} new event(s) found!")
        send_email(new_events)
    else:
        print("✅ No new events since last check.")
        if DEBUG and _debug_screenshots:
            send_debug_email()

    save_known_events(known | {ev["title"] for ev in scraped})
    print("💾 Snapshot updated")


asyncio.run(main())
