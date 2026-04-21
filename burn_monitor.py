"""Bittensor subnet incentive burn monitor — hourly Telegram reports.

Fetches all subnet data from the taostats.io API every hour.
Reports subnets NOT at 100% incentive burn, sorted by ascending burn rate.
Tracks burn rate changes between checks using a local JSON file.
"""

import json
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("burn_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TAOSTATS_API = "https://api.taostats.io/api/subnet/latest/v1"
TAOSTATS_IDENTITY_API = "https://api.taostats.io/api/subnet/identity/v1"
METAGRAPH_URL = "https://taostats.io/subnets/{netuid}/metagraph"
DATA_FILE = Path(__file__).parent / "burn_data.json"
CHECK_INTERVAL = 3600  # 1 hour

TAOSTATS_API_KEY = os.getenv("TAOSTATS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_data() -> dict:
    """Load previous snapshot from JSON file."""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.error("Failed to load data file: %s", e)
    return {}


def save_data(data: dict):
    """Persist current snapshot to JSON file."""
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error("Failed to save data file: %s", e)


def fetch_subnets() -> list[dict]:
    """Fetch all subnets from the taostats API (single call)."""
    log.info("Fetching subnet data from taostats API …")
    headers = {"Authorization": TAOSTATS_API_KEY} if TAOSTATS_API_KEY else {}
    resp = requests.get(TAOSTATS_API, headers=headers, params={"per_page": 1024}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    subnets = payload.get("data", [])
    log.info("Fetched %d subnets", len(subnets))
    return subnets


def fetch_subnet_names() -> dict[int, str]:
    """Fetch subnet names from the identity endpoint. Returns {netuid: name}."""
    headers = {"Authorization": TAOSTATS_API_KEY} if TAOSTATS_API_KEY else {}
    resp = requests.get(TAOSTATS_IDENTITY_API, headers=headers, params={"per_page": 1024}, timeout=60)
    resp.raise_for_status()
    names: dict[int, str] = {}
    for entry in resp.json().get("data", []):
        netuid = int(entry["netuid"])
        name = entry.get("subnet_name") or ""
        if name:
            names[netuid] = name
    return names


def build_snapshot(subnets: list[dict], names: dict[int, str]) -> dict:
    """Build a {netuid: info} snapshot from raw API data."""
    snap: dict = {}
    for s in subnets:
        netuid = int(s["netuid"])
        burn = float(s.get("incentive_burn", 0))
        name = names.get(netuid) or f"SN{netuid}"
        snap[str(netuid)] = {
            "incentive_burn": burn,
            "name": name,
        }
    return snap


def not_full_burn(snap: dict) -> list[tuple[int, dict]]:
    """Return subnets where incentive_burn < 1, sorted by ascending burn rate."""
    items = []
    for nid, info in snap.items():
        if float(info["incentive_burn"]) < 1.0:
            items.append((int(nid), info))
    items.sort(key=lambda x: x[1]["incentive_burn"])
    return items


def format_burn_pct(val: float) -> str:
    """Format burn rate as a readable percentage string."""
    pct = val * 100
    if pct == 0:
        return "0%"
    if pct >= 99.995:
        return "100%"
    if pct >= 10:
        return f"{pct:.1f}%"
    return f"{pct:.2f}%"


def build_report(snap: dict, prev_snap: dict) -> str:
    """Build the Telegram report message."""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    items = not_full_burn(snap)

    total_subnets = len(snap)
    full_burn = total_subnets - len(items)

    lines: list[str] = []
    lines.append("🔥 <b>Burn Rate Report</b>")
    lines.append(f"📅 {now}")
    lines.append(f"✅ Full burn (100%): {full_burn}/{total_subnets}")
    lines.append(f"⚠️ Not full burn: {len(items)} subnets\n")
    for netuid, info in items:
        burn = info["incentive_burn"]
        name = info.get("name", f"SN{netuid}")
        link = METAGRAPH_URL.format(netuid=netuid)

        # Compute change from previous snapshot
        prev = prev_snap.get(str(netuid))
        if prev is not None:
            old_burn = float(prev.get("incentive_burn", prev.get("burn_rate", 0)))
            delta = burn - old_burn
            if abs(delta) < 0.00005:
                delta_str = "—"
            elif delta > 0:
                delta_str = f"🔺+{delta*100:.2f}%"
            else:
                delta_str = f"🔻{delta*100:.2f}%"
        else:
            delta_str = "🆕"

        lines.append(f'SN{netuid} <a href="{link}">{name}</a> {format_burn_pct(burn)} {delta_str}')

    # Summary of biggest movers (burn rate changed since last check)
    movers = []
    for nid, info in snap.items():
        prev = prev_snap.get(nid)
        if prev is not None:
            old_burn = float(prev.get("incentive_burn", prev.get("burn_rate", 0)))
            delta = float(info["incentive_burn"]) - old_burn
            if abs(delta) > 0.0001:
                name = info.get("name", f"SN{nid}")
                movers.append((int(nid), name, delta))

    if movers:
        movers.sort(key=lambda x: x[2])
        lines.append("\n📊 Burn rate changes (1h):")
        for nid, name, delta in movers:
            direction = "🔺" if delta > 0 else "🔻"
            lines.append(f"  {direction} SN{nid} {name}: {delta*100:+.2f}%")

    return "\n".join(lines)


def send_telegram(text: str):
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set — printing report to stdout")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            log.info("Telegram report sent successfully")
        else:
            log.error("Telegram API error %d: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_once(prev_snap: dict) -> dict:
    """Fetch data, build report, send Telegram, return new snapshot."""
    subnets = fetch_subnets()
    names = fetch_subnet_names()
    snap = build_snapshot(subnets, names)

    report = build_report(snap, prev_snap)
    send_telegram(report)

    save_data({
        "last_check": datetime.now(JST).isoformat(),
        "subnets": snap,
    })

    return snap


def main():
    """Entry point — run every CHECK_INTERVAL seconds."""
    log.info("Burn monitor started (interval=%ds)", CHECK_INTERVAL)

    stored = load_data()
    prev_snap = stored.get("subnets", {})

    while True:
        try:
            prev_snap = run_once(prev_snap)
        except Exception as e:
            log.error("Error in check cycle: %s", e)

        log.info("Sleeping %ds until next check …", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
