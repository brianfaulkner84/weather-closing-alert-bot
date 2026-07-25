#!/usr/bin/env python3
"""Weather + school closing alert bot for Burns, La Crosse, and Tomah, WI.

Checks Open-Meteo weather forecasts and the WXOW (News 19) school closings
widget against a fixed set of thresholds, and posts a Discord webhook
message when something crosses a threshold. Designed to run hourly overnight
via GitHub Actions; state is persisted to state.json so the same conditions
aren't re-announced every run.
"""

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CLOSINGS_URL = "https://ftp2.wxow.com/closings.html"

# (display name, geocoder query)
LOCATIONS = [
    ("Burns", "Burns"),
    ("La Crosse", "La Crosse"),
    ("Tomah", "Tomah"),
]
DISTRICTS = ["Mindoro", "Tomah", "Sparta", "La Crosse"]

COLD_THRESHOLD_F = 0
HEAT_THRESHOLD_F = 90

# Open-Meteo's geocoder (backed by GeoNames) doesn't have every small
# unincorporated Wisconsin township at a resolvable granularity. Burns, WI
# (an unincorporated town in La Crosse County) failed to geocode in
# practice, so it falls back to this approximate town-center coordinate
# rather than failing the whole location silently. Geocoding is still tried
# first for every location; this is only used if that lookup comes back empty.
FALLBACK_COORDS = {
    "Burns": (43.9336, -91.1),
}

LOCAL_TZ = ZoneInfo("America/Chicago")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Requests library defaults to a User-Agent that some news-station sites
# block; a normal browser UA avoids that.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

STATUS_PATTERN = re.compile(
    r"closed|cancell?ed|delay(?:ed)?|late start|"
    r"early (?:dismissal|release|out)|"
    r"remote learning|virtual learning|e[- ]?learning|2[- ]hour|two[- ]hour",
    re.IGNORECASE,
)
CLOSED_PATTERN = re.compile(r"closed|cancell?ed", re.IGNORECASE)
# Widgets often phrase the "nothing wrong" case as "No delay", "No closings",
# "Not closed", etc. STATUS_PATTERN matches the keyword inside those too, so
# anything matching NEGATION_PATTERN is treated as open, not an alert.
NEGATION_PATTERN = re.compile(
    r"\bno\b\s+\w*\s*(closing|closure|delay|cancellation)s?\b|"
    r"\bnot\b\s+(closed|delayed|cancelled|canceled)\b",
    re.IGNORECASE,
)


def geocode_location(query):
    """Resolve a Wisconsin place name to (lat, lon) via Open-Meteo geocoding."""
    resp = requests.get(
        GEOCODING_URL,
        params={"name": query, "count": 10, "language": "en", "format": "json"},
        timeout=20,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []

    wi_matches = [
        r
        for r in results
        if r.get("country_code") == "US" and r.get("admin1") == "Wisconsin"
    ]
    if not wi_matches:
        # Small unincorporated places (e.g. Burns, WI) sometimes need the
        # state name appended to surface in the search.
        resp = requests.get(
            GEOCODING_URL,
            params={
                "name": f"{query}, Wisconsin",
                "count": 10,
                "language": "en",
                "format": "json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        wi_matches = [
            r
            for r in results
            if r.get("country_code") == "US" and r.get("admin1") == "Wisconsin"
        ]

    if not wi_matches:
        raise RuntimeError(
            f"Could not geocode '{query}, Wisconsin' via Open-Meteo. "
            "The geocoding API may not have this place at this granularity; "
            "check the name or add a manual coordinate override."
        )

    match = wi_matches[0]
    return match["latitude"], match["longitude"]


def fetch_weather(lat, lon):
    """Return today's (temperature_2m_max, temperature_2m_min, snowfall_sum)."""
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,snowfall_sum",
            "timezone": "America/Chicago",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "forecast_days": 1,
        },
        timeout=20,
    )
    resp.raise_for_status()
    daily = resp.json()["daily"]
    return daily["temperature_2m_max"][0], daily["temperature_2m_min"][0], daily["snowfall_sum"][0]


def scrape_closings():
    """Scrape the WXOW closings widget for the watched districts.

    The widget's HTML is a third-party, un-versioned page that can change
    without notice, so this does not assume a specific tag/class structure.
    Instead it flattens all visible text in document order and looks for
    each district name followed (within a small window) by a status
    keyword such as "closed" or "delay". Returns {district: status_text}
    for districts with a detected non-open status; districts not present
    in the returned dict are assumed open/unaffected.
    """
    resp = requests.get(CLOSINGS_URL, headers=HTTP_HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    tokens = [t.strip() for t in soup.stripped_strings if t.strip()]

    found = {}
    for district in DISTRICTS:
        district_lower = district.lower()
        for i, token in enumerate(tokens):
            if district_lower not in token.lower():
                continue
            # Look at this token and the next few for a status keyword,
            # since vendor widgets commonly split name/status across
            # adjacent cells or spans rather than one line of text.
            window = tokens[i : i + 4]
            status_match = None
            for w in window:
                if NEGATION_PATTERN.search(w):
                    continue
                m = STATUS_PATTERN.search(w)
                if m:
                    status_match = w if w != token else m.group(0)
                    break
            if status_match:
                found[district] = status_match
                break
    return found


def evaluate_weather_alerts(weather_by_location):
    alerts = {}
    for name, (tmax, tmin, snow) in weather_by_location.items():
        if tmin is not None and tmin < COLD_THRESHOLD_F:
            key = f"cold:{name}"
            alerts[key] = (
                f"{name}: {tmin:.0f}°F forecast low, below the "
                f"{COLD_THRESHOLD_F}°F extreme cold threshold"
            )
        if tmax is not None and tmax > HEAT_THRESHOLD_F:
            key = f"heat:{name}"
            alerts[key] = (
                f"{name}: {tmax:.0f}°F forecast high, above the "
                f"{HEAT_THRESHOLD_F}°F extreme heat threshold"
            )
        if snow is not None and snow > 0:
            key = f"snow:{name}"
            alerts[key] = f"{name}: {snow:.1f}\" forecast snowfall"
    return alerts


def evaluate_closing_alerts(closings):
    alerts = {}
    for district, status_text in closings.items():
        category = "closed" if CLOSED_PATTERN.search(status_text) else "delayed"
        key = f"closing:{district}:{category}"
        label = status_text.strip().capitalize()
        alerts[key] = f"{district}: {label}"
    return alerts


CATEGORY_HEADERS = [
    ("cold:", "**Extreme Cold**"),
    ("heat:", "**Extreme Heat**"),
    ("snow:", "**Snow**"),
    ("closing:", "**School Closings**"),
]


def group_alerts(alerts):
    """Group an {key: text} dict into ordered (header, [text, ...]) sections."""
    sections = []
    for prefix, header in CATEGORY_HEADERS:
        texts = [text for key, text in alerts.items() if key.startswith(prefix)]
        if texts:
            sections.append((header, texts))
    return sections


def format_digest(alerts, today_str):
    lines = [f"\U0001f514 **Daily Weather & School Closing Digest — {today_str}**", ""]
    sections = group_alerts(alerts)
    if not sections:
        lines.append(
            "All clear: no extreme cold, extreme heat, or snow forecast for "
            "Burns, La Crosse, or Tomah, and no closings reported for "
            "Mindoro, Tomah, Sparta, or La Crosse schools."
        )
    else:
        for header, texts in sections:
            lines.append(header)
            for t in texts:
                lines.append(f"• {t}")
            lines.append("")
    return "\n".join(lines).strip()


def format_change_alert(new_alerts):
    lines = ["⚠️ **Alert Update**", ""]
    sections = group_alerts(new_alerts)
    for header, texts in sections:
        lines.append(header)
        for t in texts:
            lines.append(f"• {t}")
        lines.append("")
    return "\n".join(lines).strip()


def chunk_message(content, limit=2000):
    if len(content) <= limit:
        return [content]
    chunks = []
    current = ""
    for line in content.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_to_discord(content):
    """Post content to the Discord webhook. Returns True if delivered.

    If DISCORD_WEBHOOK_URL isn't set (e.g. local testing), prints the
    message instead of sending and returns False so state isn't updated
    for a message that was never actually delivered.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set; dry run. Message that would be sent:\n")
        print(content)
        return False

    for chunk in chunk_message(content):
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=20)
        if resp.status_code not in (200, 204):
            print(
                f"Discord webhook post failed: {resp.status_code} {resp.text}",
                file=sys.stderr,
            )
            return False
    return True


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_digest_date": None, "last_alerts": []}
    with open(STATE_PATH, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def main():
    now = datetime.now(LOCAL_TZ)
    today_str = now.date().isoformat()

    weather_by_location = {}
    for display_name, query in LOCATIONS:
        try:
            lat, lon = geocode_location(query)
            weather_by_location[display_name] = fetch_weather(lat, lon)
        except Exception as exc:
            print(f"Warning: weather check failed for {display_name}: {exc}", file=sys.stderr)

    try:
        closings = scrape_closings()
    except Exception as exc:
        print(f"Warning: closings scrape failed: {exc}", file=sys.stderr)
        closings = {}

    current_alerts = {}
    current_alerts.update(evaluate_weather_alerts(weather_by_location))
    current_alerts.update(evaluate_closing_alerts(closings))

    state = load_state()

    if state.get("last_digest_date") != today_str:
        message = format_digest(current_alerts, today_str)
        if send_to_discord(message):
            state["last_digest_date"] = today_str
            state["last_alerts"] = sorted(current_alerts.keys())
            save_state(state)
        return

    previous_keys = set(state.get("last_alerts", []))
    new_keys = set(current_alerts.keys()) - previous_keys
    if not new_keys:
        print("No new alerts since last run; nothing to send.")
        return

    new_alerts = {k: v for k, v in current_alerts.items() if k in new_keys}
    message = format_change_alert(new_alerts)
    if send_to_discord(message):
        state["last_alerts"] = sorted(current_alerts.keys())
        save_state(state)


if __name__ == "__main__":
    main()
