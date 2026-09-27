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
# 3. Utility Functions
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


def extract_available_seats(seat_layout_res):
    areas = seat_layout_res.get("seatLayout", {}).get("colAreas", {}).get("objArea", [])
    seats_by_price = defaultdict(int)
    total_avail = 0

    for area in areas:
        for row in area.get("objRow", []):
            for seat in row.get("objSeat", []):
                if str(seat.get("SeatStatus")) == "0":
                    price = float(seat.get("price") or area.get("AreaPrice") or 0.0)
                    seats_by_price[price] += 1
                    total_avail += 1

    return total_avail, seats_by_price


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
        message_text = f"{movie_title} tickets are live at {theater_name}! ({avail_seats} seats left)"
        
        try:
            requests.post(
                ntfy_url,
                data=message_text.encode("utf-8"),  # Encode body as UTF-8 bytes
                headers={
                    "Title": title_text.encode("utf-8"),  # Encode title header as UTF-8 bytes
                    "Priority": "5",                      # Priority 5 breaks through DND mode
                    "Tags": "ticket,rotating_light,popcorn",  # ntfy renders 'rotating_light' tag as 🚨
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

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

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

    # Expand rules across all theaters if theater_id == "ALL"
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

                logger.info(f"  📽️ Movie: '{scraped_movie_title}' ({len(sessions)} total sessions scheduled)")

                for rule in user_rules:
                    rule_movie = rule["movie_name"].strip()

                    # Filter 1: Check Movie match OR "ALL" wildcard match
                    if rule_movie.upper() != "ALL" and rule_movie.lower() not in scraped_movie_title.lower():
                        logger.debug(f"    ⏭️ Skipping movie '{scraped_movie_title}' (Rule target: '{rule_movie}')")
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

                        # Filter 2: Ignore past shows
                        if session_dt_ist <= now_ist:
                            logger.info(f"    ⏳ [Session {sid} @ {time_display}] SKIPPED: Show time already passed (Current time: {now_ist.strftime('%I:%M %p')})")
                            continue

                        # Filter 3: Check Time Window Range
                        rule_start_t = time.fromisoformat(rule["start_time"])
                        rule_end_t = time.fromisoformat(rule["end_time"])
                        session_t = session_dt_ist.time()

                        if not (rule_start_t <= session_t <= rule_end_t):
                            logger.info(f"    🕒 [Session {sid} @ {time_display}] SKIPPED: Outside rule time window ({rule_start_t}-{rule_end_t})")
                            continue

                        # Construct API layout payload
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

                            tot_avail, seats_by_price = extract_available_seats(res_json)

                            if tot_avail <= 0:
                                logger.info(f"    🔴 [Session {sid} @ {time_display}] SKIPPED: 0 available seats found.")
                                continue

                            min_p = float(rule["min_price"])
                            max_p = float(rule["max_price"])

                            valid_seats = {
                                p: count
                                for p, count in seats_by_price.items()
                                if min_p <= p <= max_p
                            }

                            if not valid_seats:
                                logger.info(f"    💵 [Session {sid} @ {time_display}] SKIPPED: Seats exist ({tot_avail}), but outside price range ₹{min_p}-₹{max_p}")
                                continue

                            lowest_price = min(valid_seats.keys())

                            session_info = {
                                "session_id": sid,
                                "show_time_ist": session_dt_ist,
                                "screen": sess.get("audi") or "Screen",
                                "total_available": tot_avail,
                                "lowest_price": lowest_price,
                            }

                            logger.info(f"    🎯 MATCH FOUND! Sending notification for '{scraped_movie_title}' @ {time_display} (Seats: {tot_avail}, Lowest Price: ₹{lowest_price})")
                            dispatch_notifications(rule, session_info, cinema_name, scraped_movie_title, telegram_bot_token)

                        except Exception as e:
                            logger.error(f"    ❌ Error querying layout for session {sid}: {e}")

                        pytime.sleep(CONFIG["api"]["request_delay_seconds"])

        except Exception as e:
            logger.error(f"Error processing cinema {theater_url}: {e}")


# ---------------------------------------------------------------------------
# 5. Modal App Setup & Cron Deployment
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
    timeout=55,
)
def scheduled_ticket_check():
    logger.info("Modal Cron triggered.")
    run_worker_cycle()


@app.local_entrypoint()
def main():
    logger.info("Executing local trigger call on Modal Cloud...")
    scheduled_ticket_check.remote()