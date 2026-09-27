from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import html
import json
import logging
import os
import re
import subprocess
import sys
import time as pytime
import uuid

import modal

# ---------------------------------------------------------------------------
# 1. Logging & Timezone Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("DistrictModalWorker")

IST = timezone(timedelta(hours=5, minutes=30))

# In-memory alert cache to prevent sending duplicate notifications for the same session
NOTIFIED_SESSIONS = set()

# ---------------------------------------------------------------------------
# 2. District API Configurations & Theater Map
# ---------------------------------------------------------------------------
CONFIG = {
    "api": {
        "seat_layout_url": "https://www.district.in/gw/consumer/movies/v1/select-seat?version=3&site_id=1&channel=web&child_site_id=1&platform=district",
        "request_delay_seconds": 0.5,
    },
    "headers": {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.5",
        "api_source": "district",
        "content-type": "application/json; charset=utf-8",
        "origin": "https://www.district.in",
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "x-app-type": "ed_web",
        "x-guest-token": "1789315110452_201031861670953000_bdnj7vr357",
    },
    "cookies": {
        "AKA_A2": "A",
        "x-device-id": "c6a00fb3-f235-475c-8f13-12395eb464aa",
    },
}

THEATERS_URL_MAP = {
    "CD1020778": "https://www.district.in/movies/inox-the-marina-mall-omr-egatoor-chennai-in-chennai-CD1020778",
    "CD9505": "https://www.district.in/movies/cinepolis-bsr-mall-omr-thoraipakkam-chennai-in-chennai-CD9505",
    "CD1030432": "https://www.district.in/movies/miraj-cinemas-sekaran-mall-perrumbakkam-in-chennai-CD1030432",
}

# ---------------------------------------------------------------------------
# 3. Utility Functions & Accurate Seat Extractor from main.py
# ---------------------------------------------------------------------------
def parse_to_ist(show_time_str: str) -> datetime:
    clean_str = show_time_str.replace("Z", "")
    utc_dt = datetime.fromisoformat(clean_str).replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(IST)


def run_curl(url, headers=None, cookies=None, post_data=None):
    cmd = ["curl", "-sL", "--compressed", url]

    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])

    if cookies:
        cookie_header = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        cmd.extend(["-b", cookie_header])

    if post_data is not None:
        cmd.extend(["--data-raw", json.dumps(post_data, ensure_ascii=False)])

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def fetch_cinema_upstream(cinema_url):
    html_res = run_curl(cinema_url, headers=CONFIG["headers"])
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_res,
    )
    if not match:
        raise ValueError(f"Could not extract __NEXT_DATA__ payload from {cinema_url}")
    return json.loads(match.group(1))


def extract_price_breakdown(seat_layout_res):
    """Accurate seat availability parser ported directly from main.py."""
    available_by_price = defaultdict(list)
    total_by_price = defaultdict(int)

    fallback_price_map = {}
    sessions_meta = seat_layout_res.get("cinemaInfo", {}).get("sessions", [])
    if sessions_meta and isinstance(sessions_meta, list):
        for area_summary in sessions_meta[0].get("areas", []):
            code = area_summary.get("code")
            price = area_summary.get("price")
            if code and price is not None:
                fallback_price_map[str(code).upper()] = float(price)

    areas = seat_layout_res.get("seatLayout", {}).get("colAreas", {}).get("objArea", [])

    for area in areas:
        area_code = str(area.get("AreaCode") or area.get("AreaDesc") or "").upper()

        default_price = area.get("AreaPrice")
        if default_price is None:
            default_price = fallback_price_map.get(area_code, 0.0)
        else:
            default_price = float(default_price)

        default_desc = area.get("AreaDesc") or area_code or "STANDARD"
        overrides = area.get("overrideSeatCategory") or []

        for row in area.get("objRow", []):
            row_id = row.get("PhyRowId", "")
            for seat in row.get("objSeat", []):
                grid_num = seat.get("GridSeatNum")
                seat_num = seat.get("displaySeatNumber", "")
                seat_label = f"{row_id}{seat_num}"
                status = str(seat.get("SeatStatus"))

                price = default_price
                category = default_desc

                if seat.get("price") is not None:
                    price = float(seat.get("price"))

                if grid_num is not None and isinstance(overrides, list):
                    for ov in overrides:
                        start = ov.get("GridSeatNumStart")
                        end = ov.get("GridSeatNumEnd")
                        if start is not None and end is not None and start <= grid_num <= end:
                            if ov.get("AreaPrice") is not None:
                                price = float(ov.get("AreaPrice"))
                            category = ov.get("AreaDesc") or ov.get("AreaCode") or category
                            break

                final_price = float(price) if price is not None else 0.0
                key = (final_price, str(category))

                total_by_price[key] += 1
                if status == "0":
                    available_by_price[key].append(seat_label)

    breakdown = []
    total_avail = 0

    all_keys = sorted(
        set(list(available_by_price.keys()) + list(total_by_price.keys())),
        key=lambda x: (x[0] if x[0] is not None else 0.0, str(x[1])),
    )

    for price, category in all_keys:
        seats = available_by_price.get((price, category), [])
        avail_count = len(seats)
        total_avail += avail_count
        if avail_count > 0:
            breakdown.append(
                {
                    "category": category,
                    "price": price,
                    "available_count": avail_count,
                }
            )

    return total_avail, breakdown


def dispatch_notifications(user_alert, session_info, theater_name, movie_title, telegram_bot_token):
    import requests

    telegram_id = user_alert["telegram_id"]
    ntfy_topic = user_alert.get("ntfy_topic")

    show_dt = session_info["show_time_ist"]
    time_str = show_dt.strftime("%d %b, %I:%M %p")
    avail_seats = session_info["total_available"]
    screen_name = session_info["screen"]
    lowest_price = session_info["lowest_price"]

    msg = (
        f"🚨 <b>TICKET OPENING ALERT!</b>\n\n"
        f"🎬 <b>Movie:</b> {html.escape(movie_title)}\n"
        f"📍 <b>Theater:</b> {html.escape(theater_name)}\n"
        f"🕒 <b>Showtime:</b> {time_str} IST\n"
        f"🎟️ <b>Screen:</b> {html.escape(screen_name)}\n"
        f"🟢 <b>Available Seats:</b> {avail_seats} seats left!\n"
        f"💰 <b>Starting Price:</b> ₹{lowest_price}\n\n"
        f"⚡ <i>Book now on District before seats run out!</i>"
    )

    # Telegram Notification
    tg_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {"chat_id": telegram_id, "text": msg, "parse_mode": "HTML"}
    try:
        res = requests.post(tg_url, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info(f"✅ Telegram alert sent to User {telegram_id} for '{movie_title}' at {theater_name}")
        else:
            logger.error(f"❌ Telegram API Error: {res.text}")
    except Exception as e:
        logger.error(f"❌ Failed to send Telegram alert: {e}")

    # Backup ntfy Push Notification
    if ntfy_topic:
        ntfy_url = f"https://ntfy.sh/{ntfy_topic}"
        title_text = f"TICKET ALERT: {movie_title}"
        message_text = f"{movie_title} tickets live at {theater_name}! ({avail_seats} seats left)"
        try:
            requests.post(
                ntfy_url,
                data=message_text.encode("utf-8"),
                headers={
                    "Title": title_text.encode("utf-8"),
                    "Priority": "5",
                    "Tags": "ticket,rotating_light",
                },
                timeout=10,
            )
            logger.info(f"✅ ntfy push dispatched to topic: '{ntfy_topic}'")
        except Exception as e:
            logger.error(f"❌ Failed to send ntfy push alert: {e}")


# ---------------------------------------------------------------------------
# 4. Scraper Logic Execution
# ---------------------------------------------------------------------------
def run_worker_cycle():
    from dotenv import load_dotenv
    import requests
    from supabase import Client, create_client

    load_dotenv()

    supabase_url = os.environ.get("SUPABASE_URL", "https://imrvfqbadzhtzfrsmcff.supabase.co")
    supabase_key = os.environ.get("SUPABASE_KEY")
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not supabase_key or not telegram_bot_token:
        logger.critical("Missing required environment variables (SUPABASE_KEY / TELEGRAM_BOT_TOKEN) in Modal Secrets.")
        return

    supabase: Client = create_client(supabase_url, supabase_key)

    now_ist = datetime.now(IST)
    logger.info("=" * 60)
    logger.info(f"Starting Ticket Check Run: {now_ist.strftime('%Y-%m-%d %I:%M:%S %p')} IST")

    try:
        res = supabase.table("user_alerts").select("*").eq("is_active", True).execute()
        active_alerts = res.data
    except Exception as e:
        logger.critical(f"Failed to fetch user_alerts from Supabase: {e}")
        return

    if not active_alerts:
        logger.info("No active alerts configured in Supabase. Ending cycle.")
        return

    logger.info(f"Loaded {len(active_alerts)} active user monitor rules.")

    expanded_rules_by_theater = defaultdict(list)
    all_theater_ids = list(THEATERS_URL_MAP.keys())

    for alert in active_alerts:
        target_t = alert["theater_id"]
        if target_t.upper() == "ALL":
            for tid in all_theater_ids:
                expanded_rules_by_theater[tid].append(alert)
        else:
            expanded_rules_by_theater[target_t].append(alert)

    for theater_id, user_rules in expanded_rules_by_theater.items():
        theater_url = THEATERS_URL_MAP.get(theater_id)
        if not theater_url:
            logger.warning(f"Theater ID '{theater_id}' not mapped to URL. Skipping.")
            continue

        try:
            upstream = fetch_cinema_upstream(theater_url)
            page_props = upstream.get("props", {}).get("pageProps", {}).get("data", {})

            cinema_id = str(page_props.get("cinemaId"))
            cinema_name = page_props.get("cinemaName") or "Theater"
            city_name = page_props.get("cityName", "chennai")
            arranged = (
                page_props.get("serverState", {})
                .get(cinema_id, {})
                .get("arrangedSessions", [])
            )

            logger.info(f"📍 Connected to Cinema: {cinema_name} (ID: {cinema_id}) | Found {len(arranged)} Movies")

            for movie_entry in arranged:
                m_data = movie_entry.get("data", {})
                scraped_movie_title = m_data.get("name") or movie_entry.get("entityName") or "Unknown"
                content_id = str(m_data.get("contentId") or movie_entry.get("entityCode"))
                sessions = movie_entry.get("sessions", [])

                for rule in user_rules:
                    rule_movie = rule["movie_name"].strip()

                    if rule_movie.upper() != "ALL" and rule_movie.lower() not in scraped_movie_title.lower():
                        continue

                    for sess in sessions:
                        sid = str(sess.get("sid"))
                        show_time_str = sess.get("showTime", "")
                        if not show_time_str:
                            continue

                        try:
                            session_dt_ist = parse_to_ist(show_time_str)
                        except Exception:
                            continue

                        time_display = session_dt_ist.strftime("%I:%M %p (%d %b)")

                        # Filter 1: Drop past shows
                        if session_dt_ist <= now_ist:
                            continue

                        # Filter 2: Time Window check
                        rule_start_t = time.fromisoformat(rule["start_time"])
                        rule_end_t = time.fromisoformat(rule["end_time"])
                        session_t = session_dt_ist.time()

                        if not (rule_start_t <= session_t <= rule_end_t):
                            continue

                        # Dedup Check
                        session_key = f"{rule['id']}_{sid}"
                        if session_key in NOTIFIED_SESSIONS:
                            logger.info(f"    ⏭️ [Session {sid} @ {time_display}] Already notified user previously. Skipping.")
                            continue

                        fmt_code = sess.get("entityDataCode") or sess.get("fid", "").lower()
                        payload = {
                            "cinemaId": int(sess.get("cid", cinema_id)),
                            "sessionId": sid,
                            "providerId": int(sess.get("pid", 1707)),
                            "screenOnTop": True,
                            "freeSeating": "false",
                            "screenFormat": sess.get("scrnFmt", "2D"),
                            "moviecode": sess.get("mid"),
                            "config": {"socialDistancing": 1},
                            "retrieve": False,
                            "contentId": content_id,
                            "cityKey": city_name,
                            "sessionDate": show_time_str.split("T")[0],
                            "fetchSessions": True,
                            "movieFormatCode": fmt_code,
                        }

                        req_headers = dict(CONFIG["headers"])
                        req_headers["paytmcinemaid"] = cinema_id
                        req_headers["x-request-id"] = str(uuid.uuid4())

                        try:
                            res_text = run_curl(
                                CONFIG["api"]["seat_layout_url"],
                                headers=req_headers,
                                cookies=CONFIG["cookies"],
                                post_data=payload,
                            )
                            res_json = json.loads(res_text)

                            tot_avail, breakdown = extract_price_breakdown(res_json)

                            if tot_avail <= 0:
                                logger.info(f"    🔴 [{scraped_movie_title} @ {time_display}] 0 open seats available.")
                                continue

                            min_p = float(rule["min_price"])
                            max_p = float(rule["max_price"])

                            valid_tiers = [
                                b for b in breakdown
                                if min_p <= float(b["price"]) <= max_p and b["available_count"] > 0
                            ]

                            if not valid_tiers:
                                logger.info(f"    💵 [{scraped_movie_title} @ {time_display}] Seats exist ({tot_avail}), but outside price range ₹{min_p}-₹{max_p}")
                                continue

                            lowest_price = min(b["price"] for b in valid_tiers)

                            session_info = {
                                "session_id": sid,
                                "show_time_ist": session_dt_ist,
                                "screen": sess.get("audi") or "Screen",
                                "total_available": tot_avail,
                                "lowest_price": lowest_price,
                            }

                            logger.info(f"    🎯 MATCH FOUND! Dispatched for '{scraped_movie_title}' @ {time_display} (Seats: {tot_avail}, Starting Price: ₹{lowest_price})")
                            dispatch_notifications(rule, session_info, cinema_name, scraped_movie_title, telegram_bot_token)
                            NOTIFIED_SESSIONS.add(session_key)

                        except Exception as e:
                            logger.error(f"    ❌ Error querying layout for session {sid}: {e}")

                        pytime.sleep(CONFIG["api"]["request_delay_seconds"])

        except Exception as e:
            logger.error(f"Error processing cinema {theater_url}: {e}")


# ---------------------------------------------------------------------------
# 5. Modal Container Setup (With tele-scheduler secret)
# ---------------------------------------------------------------------------
app_image = (
    modal.Image.debian_slim()
    .apt_install("curl")
    .pip_install("requests", "supabase", "python-dotenv")
)

app = modal.App("district-ticket-worker")


@app.function(
    image=app_image,
    schedule=modal.Cron("*/1 * * * *"),
    secrets=[modal.Secret.from_name("tele-scheduler")],
    timeout=55,
)
def scheduled_ticket_check():
    logger.info("Modal Cron triggered.")
    run_worker_cycle()


@app.local_entrypoint()
def main():
    logger.info("Executing local trigger call on Modal Cloud...")
    scheduled_ticket_check.remote()