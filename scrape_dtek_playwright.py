#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DTEK-KEM schedule scraper (Playwright, Python) — resilient selectors
- Bypasses cookie/attention overlays
- Works whether the form is in main DOM or an <iframe>
- Uses label/placeholder/name/id fallbacks for street/house
- Forces both tabs to render (only to trigger table load; ignores .date.active)
- Parses only .discon-fact-table[rel] (truth source)
- Extracts 24 hourly cells; maps classes -> statuses
- Date-aware selection (Europe/Kyiv)
- Writes full JSON + selected subset, and prints the selected days
- --debug prints helpful diagnostics

Usage:
  python scrape_dtek_playwright.py --street "Здановської Ю" --house "85" --out dtek_out.json --headful --debug
"""

import json
import sys
import argparse
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from zoneinfo import ZoneInfo
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

UTC = timezone.utc
URL = "https://www.dtek-kem.com.ua/ua/shutdowns"
TZ_KYIV = ZoneInfo("Europe/Kyiv")

# ----------------- Logging   ----------------

logging.basicConfig(
    level=logging.INFO,                   # or DEBUG for more verbosity
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]   # print to stdout (FastAPI will capture it)
)

log = logging.getLogger("dtek")


# ----------------- Utilities -----------------

def status_from_class(cls: str | None) -> str:
    cls = (cls or "").strip()
    if "cell-scheduled" in cls:
        return "off_full_hour"
    if "cell-non-scheduled" in cls:
        return "light_on"
    if "cell-first-half" in cls:
        return "off_first_30"
    if "cell-second-half" in cls:
        return "off_second_30"
    return "unknown"

def is_all_light_on(slots: list[dict]) -> bool:
    """Return True if all 24 hourly slots are 'light_on'."""
    return len(slots) == 24 and all(s.get("status") == "light_on" for s in slots)

def to_iso_uk_datetime(text: str | None) -> str | None:
    if not text:
        return None
    import re
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", text.strip())
    if not m:
        return None
    dd, MM, yyyy, hh, mm = map(int, m.groups())
    dt = datetime(yyyy, MM, dd, hh, mm)
    return dt.isoformat()


def ts_to_kyiv_date_str(ts: str | int) -> str:
    ts_int = int(ts)
    dt = datetime.fromtimestamp(ts_int, TZ_KYIV)
    return dt.strftime("%Y-%m-%d")


def select_days_to_print(schedule_by_date: dict[str, list[dict]]) -> list[str]:
    today = datetime.now(TZ_KYIV).date()
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)

    days = set(schedule_by_date.keys())
    s_today = today.strftime("%Y-%m-%d")
    s_tomorrow = tomorrow.strftime("%Y-%m-%d")
    s_yesterday = yesterday.strftime("%Y-%m-%d")

    if s_today in days and s_tomorrow in days:
        return [s_today, s_tomorrow]
    if s_yesterday in days and s_today in days:
        return [s_today]
    if s_today in days:
        return [s_today]
    if s_tomorrow in days:
        return [s_tomorrow]
    if s_yesterday in days:
        return [s_yesterday]
    return sorted(days)


def summarize_day_slots(slots: list[dict]) -> str:
    return "\n".join(f"{h['hour']:02d}–{h['hour']+1:02d}: {h['status']}" for h in slots)


# ----------------- Page helpers -----------------

def nuke_overlays(page, debug=False):
    """Remove or accept cookie/attention overlays that can block clicks."""
    with suppress(Exception):
        # Try explicit accept buttons (common UA text)
        for text in ["Приймаю", "Згоден", "Погоджуюсь", "ОК", "Ok", "OK"]:
            btn = page.get_by_role("button", name=text)
            if btn and btn.count() > 0:
                btn.first.click(timeout=1000)
                if debug:
                    log.debug(f"[debug] clicked consent button: {text}")
                    #print(f"[debug] clicked consent button: {text}")
                break
    # Hard remove known overlays by selectors
    page.evaluate("""
        const sels = [
          '#modal-attention', '#modalCookie', '#cookie', '#cookies', '.cookie', '.cookie-block',
          '.cookie-consent', '.modal-cookie', '[data-testid="cookie"]'
        ];
        for (const sel of sels) {
          document.querySelectorAll(sel).forEach(el => { try { el.remove(); } catch(e){} });
        }
    """)


def find_form_context(page, debug=False):
    """
    Returns an object that supports .locator(...) for the schedule form:
      - the page itself, or
      - the frame that actually contains the form
    """
    # Quick check in main DOM
    if page.locator("#street").count() > 0 or page.locator(".discon-schedule-form").count() > 0:
        if debug:
            #print("[debug] form found in main DOM")
            log.debug("[debug] form found in main DOM")
        return page

    # Search all frames for the form
    for fr in page.frames:
        try:
            if fr.locator("#street").count() > 0 or fr.locator(".discon-schedule-form").count() > 0:
                if debug:
                    #print("[debug] form found in frame:", fr.url)
                    log.debug("[debug] form found in frame: %s", fr.url)
                return fr
        except Exception:
            pass

    # Fallback: just return page; caller will raise if not found
    if debug:
        #print("[debug] form context not found yet")
        log.debug("[debug] form context not found yet")
    return page


def wait_for_form_ready(ctx, debug=False):
    """Wait until any of the street inputs appears and is visible."""
    candidates = [
        ".discon-schedule-form #street",
        "#street",
        "input[name='street']",
        "input[id*='street']",
        "input[placeholder*='Вулиц']",
        "input[placeholder*='Адрес']",
        "input[type='search']",
    ]
    last_err = None
    for sel in candidates:
        try:
            ctx.locator(sel).first.wait_for(state="visible", timeout=8000)
            if debug:
                #print(f"[debug] street input ready via selector: {sel}")
                log.debug(f"[debug] street input ready via selector: {sel}")
            return sel
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("Street input not found/visible")


def fill_address(ctx, street: str, house: str, debug=False):
    # Street
    sel = wait_for_form_ready(ctx, debug=debug)
    street_el = ctx.locator(sel).first
    log.info("Typing street: %s", street)
    # Click with retries (in case something overlays briefly)
    try:
        street_el.click(timeout=8000)
    except PWTimeout:
        # Try to scroll into view and force
        street_el.scroll_into_view_if_needed(timeout=2000)
        street_el.click(timeout=8000, force=True)
    street_el.fill("")
    street_el.type(street, delay=60)

    # Street autocomplete lists vary by build; try both
    ac_opts = [
        "#streetautocomplete-list div",
        ".autocomplete-items div",
        ".autocomplete-suggestions div",
    ]
    # Suggestions render asynchronously after typing; wait for them before counting
    with suppress(PWTimeout):
        ctx.locator(", ".join(ac_opts)).first.wait_for(state="visible", timeout=8000)
    picked = False
    for ac in ac_opts:
        count = ctx.locator(ac).count()
        log.debug("Street autocomplete count: %s", count)
        if count > 0:
            if debug:
                #print(f"[debug] street autocomplete '{ac}' count:", count)
                log.debug(f"[debug] street autocomplete '{ac}' count: {count}")
            ctx.locator(ac).first.click()
            picked = True
            break
    if not picked and debug:
        #print("[debug] street autocomplete not found; assuming direct field accepts value")
        log.debug("[debug] street autocomplete not found; assuming direct field accepts value")

    # House: similar fallback strategy
    house_candidates = [
        ".discon-schedule-form #house_num",
        "#house_num",
        "input[name='house_num']",
        "input[id*='house']",
        "input[placeholder*='Будин']",
        "input[placeholder*='Будинок']",
        "input[placeholder*='Номер будинку']",
    ]
    house_sel = None
    for sel2 in house_candidates:
        if ctx.locator(sel2).count() > 0:
            house_sel = sel2
            break
    if not house_sel:
        raise RuntimeError("House input not found; selectors outdated.")

    # Wait until house is enabled
    house_el = ctx.locator(house_sel).first
    house_el.scroll_into_view_if_needed(timeout=2000)
    house_el.wait_for(state="visible", timeout=8000)
    # Some pages disable until street chosen; poll 'disabled' property
    with suppress(Exception):
        ctx.wait_for_function("el => !!el && !el.disabled", arg=house_el.element_handle(), timeout=10000)

    try:
        house_el.click(timeout=5000)
    except PWTimeout:
        house_el.click(timeout=5000, force=True)

    house_el.fill("")
    house_el.type(house, delay=60)

    # House autocomplete
    with suppress(PWTimeout):
        ctx.locator(", ".join(ac_opts)).first.wait_for(state="visible", timeout=8000)
    picked = False
    for ac in ac_opts:
        count = ctx.locator(ac).count()
        if count > 0:
            if debug:
                #print(f"[debug] house autocomplete '{ac}' count:", count)
                log.debug(f"[debug] house autocomplete '{ac}' count: {count}")
            ctx.locator(ac).first.click()
            picked = True
            break
    if not picked and debug:
        #print("[debug] house autocomplete not found; assuming direct field accepts value")
        log.debug("[debug] house autocomplete not found; assuming direct field accepts value")


def ensure_results_loaded(ctx):
    # Either tables, or alert, or info message
    ctx.wait_for_selector(".discon-fact-tables table, .discon-schedule-alert, #showCurOutage p", timeout=20000)


def ensure_both_tabs_render(ctx, debug=False):
    log.info("Forcing both tabs to render (today/tomorrow)…")
    with suppress(Exception):
        ctx.locator(".dates .date").nth(0).click()
        ctx.wait_for_timeout(200)
    with suppress(Exception):
        ctx.locator(".dates .date").nth(1).click()
        ctx.wait_for_timeout(200)
    with suppress(Exception):
        ctx.locator(".dates .date").nth(0).click()
        ctx.wait_for_timeout(200)

    if debug:
        with suppress(Exception):
            active = ctx.locator(".dates .date.active").first
            log.debug("[debug] UI .date.active text: %s rel: %s",
                      (active.text_content() or "").strip(), active.get_attribute("rel"))
            #print("[debug] UI .date.active text:", (active.text_content() or "").strip(),
            #      "rel:", active.get_attribute("rel"))


def extract_last_update(ctx) -> str | None:
    with suppress(Exception):
        text = ctx.locator(".discon-fact-info .update").first.text_content()
        text = (text or "").strip()
        if text:
            return to_iso_uk_datetime(text)
    with suppress(Exception):
        val = ctx.locator("input[name='updateFact']").get_attribute("value")
        if val:
            return to_iso_uk_datetime(val)
    return None


# ----------------- Table extraction -----------------

def extract_schedule(ctx, debug=False) -> dict[str, list[dict]]:
    """
    Return { "<unix_ts>": [ {"hour":0,"status":"..."}, ..., {"hour":23,"status":"..."} ] }
    Only uses .discon-fact-table[rel] (truth source).
    """
    log.info("Extracting tables…")
    results: dict[str, list[dict]] = {}
    tables = ctx.locator(".discon-fact-tables .discon-fact-table")
    count = tables.count()

    if debug:
        log.debug(f"[debug] .discon-fact-table count: {count}")
        #print(f"[debug] .discon-fact-table count: {count}")

    for i in range(count):
        table = tables.nth(i)
        day_ts = table.get_attribute("rel") or ""
        # Find the hour row
        row = table.locator("table tbody tr").first
        td_nodes = row.locator("td")
        td_count = td_nodes.count()
        if td_count == 0:
            if debug:
                #print(f"[debug] table {i}: no TDs")
                log.debug(f"[debug] table {i}: no TDs")
            continue

        # Skip spacer <td colspan="2">
        first_colspan = (td_nodes.nth(0).get_attribute("colspan") or "")
        start_idx = 1 if first_colspan else 0

        hour_cells = []
        for idx in range(start_idx, td_count):
            hour_cells.append(td_nodes.nth(idx))
            if len(hour_cells) == 24:
                break

        if len(hour_cells) != 24:
            raise RuntimeError(
                f"Expected 24 hour cells, got {len(hour_cells)} for day rel={day_ts} "
                f"(total tds={td_count}, colspan at 0={'yes' if first_colspan else 'no'})"
            )

        hours = []
        for h, td in enumerate(hour_cells):
            cls = td.get_attribute("class")
            hours.append({"hour": h, "status": status_from_class(cls)})

        if day_ts:
            results[day_ts] = hours

    return results


# ----------------- Runner -----------------

def run(street: str, house: str, out_path: str, headless: bool, debug: bool):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="uk-UA",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
        )
        page = context.new_page()

        # Navigate; allow hydration to start
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)

        # Remove overlays that may block interaction
        nuke_overlays(page, debug=debug)

        # Find where the form actually is (main DOM vs iframe)
        ctx = find_form_context(page, debug=debug)

        # As a second chance, wait a bit and nuke overlays again
        with suppress(Exception):
            ctx.wait_for_timeout(500)
        nuke_overlays(page, debug=debug)

        # Fill address
        fill_address(ctx, street, house, debug=debug)

        # Wait for results & force both tables to render
        ensure_results_loaded(ctx)
        ensure_both_tabs_render(ctx, debug=debug)

        # Multi-feeders banner and outage message (keep but do not block parsing)
        multi_feeders = ctx.locator(".discon-schedule-alert").count() > 0
        info_message = None
        with suppress(Exception):
            msg = ctx.locator("#showCurOutage p").first
            if msg and (txt := (msg.text_content() or "").strip()):
                info_message = txt

        # Extract schedules if tables exist
        data = {}
        if ctx.locator(".discon-fact-tables table").count() > 0:
            data = extract_schedule(ctx, debug=debug)

        # Timestamp → Europe/Kyiv dates
        by_date: dict[str, list[dict]] = {}
        for day_ts, slots in data.items():
            by_date[ts_to_kyiv_date_str(day_ts)] = slots

        chosen_days = select_days_to_print(by_date)

        # Diagnostics: show correlation
        if debug:
            print("\n[debug] Tables (rel → date KYIV):")
            for day_ts in sorted(data.keys(), key=int):
                print(" ", day_ts, "→", ts_to_kyiv_date_str(day_ts))
            with suppress(Exception):
                d0 = ctx.locator(".dates .date").nth(0)
                d1 = ctx.locator(".dates .date").nth(1)
                print("[debug] Tabs:")
                print("  Tab[0] rel=", d0.get_attribute("rel"), "text=", (d0.text_content() or "").strip())
                print("  Tab[1] rel=", d1.get_attribute("rel"), "text=", (d1.text_content() or "").strip())
                a = ctx.locator(".dates .date.active").first
                print("  Active tab rel=", a.get_attribute("rel"), "text=", (a.text_content() or "").strip())

        # Console summary (selected only)
        if chosen_days:
            print("\n=== Selected schedule ===")
            for d in chosen_days:
                if d in by_date:
                    print(f"\n{d}:")
                    print(summarize_day_slots(by_date[d]))
                else:
                    print(f"\n{d}: (no data for this date!)")
        else:
            print("\n(No printable days matched the selection rules.)")

        
        # --- Filter out days that are "all light_on" for printing/selected ---
        filtered_days = [d for d in chosen_days if d in by_date and not is_all_light_on(by_date[d])]

        # Console summary (selected only, after filtering)
        if filtered_days:
            print("\n=== Selected schedule ===")
            for d in filtered_days:
                print(f"\n{d}:")
                print(summarize_day_slots(by_date[d]))
        else:
            print("\n(No printable days after filtering all-light-on days.)")

        # Compose JSON (keep full 'by_date', but apply filter to 'selected*')
        last_update_iso = extract_last_update(ctx)  # adjust to your variable: 'page' or 'ctx'
        selected_subset = {d: by_date[d] for d in filtered_days}


        # JSON compose
        last_update_iso = extract_last_update(ctx)
        #selected_subset = {d: by_date[d] for d in chosen_days if d in by_date}

        final = {
            "source": URL,
            "queried_at": datetime.now(UTC).isoformat(),
            "street_input": street,
            "house_input": house,
            "last_update_local": last_update_iso,
            "multiple_feeders": multi_feeders,
            "site_message": info_message,
            "raw_by_timestamp": data,
            "by_date": by_date,
            #"selected_days": chosen_days,
            "selected_days": filtered_days,
            "selected_by_date": selected_subset
        }

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(final, f, ensure_ascii=False, indent=2)

        print("\n=== JSON written ===")
        print(json.dumps(final, ensure_ascii=False, indent=2))

        browser.close()

# ----------------- API -----------------


def fetch_schedule(street: str, house: str, headless: bool = True, debug: bool = False) -> dict:
    """Return the JSON payload without printing or saving."""
    from contextlib import suppress
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    UTC = timezone.utc
    TZ_KYIV = ZoneInfo("Europe/Kyiv")

    def _inner():
        nonlocal street, house, headless, debug
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="uk-UA",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"),
            )
            page = context.new_page()
            
        # --- current flow (exactly as in run()) ---
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            nuke_overlays(page, debug=debug)
            ctx = find_form_context(page, debug=debug)
            with suppress(Exception):
                ctx.wait_for_timeout(500)
            nuke_overlays(page, debug=debug)

            fill_address(ctx, street, house, debug=debug)
            ensure_results_loaded(ctx)
            ensure_both_tabs_render(ctx, debug=debug)

            multi_feeders = ctx.locator(".discon-schedule-alert").count() > 0
            info_message = None
            with suppress(Exception):
                msg = ctx.locator("#showCurOutage p").first
                if msg and (txt := (msg.text_content() or "").strip()):
                    info_message = txt
            
            data = {}
            if ctx.locator(".discon-fact-tables table").count() > 0:
                data = extract_schedule(ctx, debug=debug)

            by_date: dict[str, list[dict]] = {
                ts_to_kyiv_date_str(ts): slots for ts, slots in data.items()
            }

            chosen_days = select_days_to_print(by_date)

            # filter out "all light_on" days for the selected subset/printing API
            def is_all_light_on(slots: list[dict]) -> bool:
                #log.info("Skipping %s because it is all light_on", d)
                return len(slots) == 24 and all(s.get("status") == "light_on" for s in slots)
            filtered_days = [d for d in chosen_days if d in by_date and not is_all_light_on(by_date[d])]

            last_update_iso = extract_last_update(ctx)
            selected_subset = {d: by_date[d] for d in filtered_days}

            final = {
                "source": URL,
                "queried_at": datetime.now(UTC).isoformat(),
                "street_input": street,
                "house_input": house,
                "last_update_local": last_update_iso,
                "multiple_feeders": multi_feeders,
                "site_message": info_message,
                "raw_by_timestamp": data,
                "by_date": by_date,
                "selected_days": filtered_days,
                "selected_by_date": selected_subset,
            }

            browser.close()
            return final

    return _inner()



# ----------------- CLI -----------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Scrape DTEK-KEM schedule by address (resilient Playwright)")
    ap.add_argument("--street", required=True, help="Street name (Ukrainian, exactly as on the site)")
    ap.add_argument("--house", required=True, help="House number")
    ap.add_argument("--out", default="dtek_out.json", help="Output JSON path")
    ap.add_argument("--headful", action="store_true", help="Run with visible browser")
    ap.add_argument("--debug", action="store_true", help="Print extra diagnostics")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(
        street=args.street,
        house=args.house,
        out_path=args.out,
        headless=not args.headful,
        debug=args.debug,
    )

