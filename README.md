# Weather + School Closing Alert Bot

Checks weather and regional school closing status for three Wisconsin
locations and posts a Discord alert when conditions cross defined
thresholds. Runs on a schedule via GitHub Actions, so it works without a
laptop being on.

## What it checks

**Weather** (via the [Open-Meteo](https://open-meteo.com/) forecast API,
today's forecast, `America/Chicago` timezone) for:

- Burns, WI
- La Crosse, WI
- Tomah, WI

Coordinates for each are resolved at runtime with Open-Meteo's free
geocoding API — nothing is hardcoded.

**School closings** for four districts, scraped from the WXOW (News 19,
Coulee Region) closings widget at `https://ftp2.wxow.com/closings.html`:

- Mindoro
- Tomah
- Sparta
- La Crosse

## Thresholds

| Condition | Trigger |
|---|---|
| Extreme cold | Forecast low below 0°F at any of the three locations |
| Extreme heat | Forecast high above 90°F at any of the three locations |
| Snow | Any nonzero forecast snowfall at any of the three locations |
| School closing | Any of the four watched districts listed as closed or delayed |

## Message behavior (state tracking)

State is persisted in `state.json`, which the workflow commits back to the
repo after any run that sends a message:

- **Daily digest**: the first run after local midnight sends one message
  covering all currently active conditions (or "all clear" if none), and
  records that today's digest was sent.
- **Change alerts**: every other run compares current conditions to the
  last saved set and sends a short message only if something new has
  appeared since the digest (e.g. a district closes later in the morning).
  Unchanged or resolved conditions are not re-announced.

## Schedule

Runs hourly, midnight through 8:00 AM Central Time, via the cron schedule
in `.github/workflows/alert.yml`. GitHub Actions cron runs in UTC, so the
schedule is expressed as `0 6-14 * * *` (UTC) for Central Standard Time
(UTC-6). **This needs manual adjustment across DST changes** — during
Central Daylight Time (UTC-5, roughly mid-March–early November) it should
be `0 5-13 * * *` instead. See the comment in the workflow file.

The workflow also has `workflow_dispatch:` enabled, so it can be triggered
manually from the Actions tab at any time (useful for a first demo run
without waiting for the schedule).

## Setup

1. **Create a Discord webhook**: in your Discord server, go to
   *Server Settings → Integrations → Webhooks → New Webhook*, pick the
   channel, and copy the webhook URL.
2. **Add it as a repo secret**: in this repository, go to
   *Settings → Secrets and variables → Actions → New repository secret*,
   name it `DISCORD_WEBHOOK_URL`, and paste the webhook URL as the value.
   The script and workflow only ever read this from the environment —
   it's never hardcoded.
3. **First run**: go to the *Actions* tab, select the "Weather & School
   Closing Alert" workflow, and use *Run workflow* to trigger it manually.

## Running locally

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python alert_bot.py
