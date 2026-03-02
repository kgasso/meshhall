"""
Plugin: Weather / NWS
Commands:
  !wx              — Latest cached forecast for configured location
  !alerts          — Active NWS alerts for configured zone
  !alert <id>      — Read full alert text
  !wxrefresh       — (Admin) Force immediate NWS data refresh

Alert sources:
  1. NWS API (api.weather.gov) — polled on schedule and on !rehash/!wxrefresh
  2. SDR SAME/EAS — written to DB by meshhall-same (separate repository),
     provides offline-capable alerts via RTL-SDR dongle

Config: config/plugins/weather.yaml
"""

__version__ = "0.4.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'nws',
    event_id    TEXT UNIQUE,
    event_type  TEXT,
    headline    TEXT,
    description TEXT,
    expires     INTEGER,
    area        TEXT,
    broadcast   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wx_alerts_ts ON wx_alerts(ts);
CREATE INDEX IF NOT EXISTS idx_wx_alerts_expires ON wx_alerts(expires);

CREATE TABLE IF NOT EXISTS wx_forecast (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    zone        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'nws',
    raw         TEXT NOT NULL
);
"""

# NWS API endpoints (api.weather.gov — alerts.weather.gov decommissioned Dec 2 2025)
NWS_ALERTS_ZONE  = "https://api.weather.gov/alerts/active/zone/{zone}"
NWS_ALERTS_POINT = "https://api.weather.gov/alerts/active?point={lat},{lon}"
NWS_POINT        = "https://api.weather.gov/points/{lat},{lon}"
NWS_FORECAST     = "https://api.weather.gov/gridpoints/{office}/{x},{y}/forecast"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _now() -> int:
    return int(time.time())


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    # All config read lazily through _cfg() so !rehash takes immediate effect.
    def _cfg():
        return config.plugin("weather")

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get(url: str) -> Optional[dict]:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"User-Agent": _cfg().get("user_agent", "MeshHall/1.0")},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    logger.debug(f"NWS HTTP {resp.status} for {url}")
        except Exception as e:
            logger.debug(f"HTTP fetch failed [{url}]: {e}")
        return None

    # ── Forecast fetch ────────────────────────────────────────────────────────

    async def _fetch_forecast():
        lat  = _cfg().get("lat")
        lon  = _cfg().get("lon")
        zone = _cfg().get("zone", "")
        if not lat or not lon:
            logger.warning("Weather: lat/lon not configured, skipping forecast fetch.")
            return
        data = await _get(NWS_POINT.format(lat=lat, lon=lon))
        if not data:
            return
        props  = data.get("properties", {})
        office = props.get("gridId")
        x, y   = props.get("gridX"), props.get("gridY")
        if not all([office, x, y]):
            logger.warning("NWS /points did not return grid info.")
            return
        forecast = await _get(NWS_FORECAST.format(office=office, x=x, y=y))
        if not forecast:
            return
        periods  = forecast.get("properties", {}).get("periods", [])[:4]
        zone_key = zone or f"{lat},{lon}"
        await db.execute(
            "INSERT INTO wx_forecast (ts, zone, source, raw) VALUES (?,?,?,?)",
            (_now(), zone_key, "nws", json.dumps(periods)),
        )
        await db.commit()
        logger.info(f"NWS forecast updated for zone {zone_key}.")

    # ── Alerts fetch ──────────────────────────────────────────────────────────

    async def _fetch_alerts():
        zone = _cfg().get("zone", "")
        lat  = _cfg().get("lat")
        lon  = _cfg().get("lon")
        if zone:
            url = NWS_ALERTS_ZONE.format(zone=zone)
        elif lat and lon:
            url = NWS_ALERTS_POINT.format(lat=lat, lon=lon)
        else:
            logger.warning("Weather: no zone or lat/lon configured, skipping alert fetch.")
            return
        data = await _get(url)
        if not data:
            return
        new_count = 0
        for f in data.get("features", []):
            props    = f.get("properties", {})
            event_id = props.get("id") or f.get("id")
            if not event_id:
                continue
            if await db.fetchone("SELECT id FROM wx_alerts WHERE event_id=?", (event_id,)):
                continue
            expires = None
            for field in ("expires", "ends", "effective"):
                exp_str = props.get(field)
                if exp_str:
                    try:
                        expires = int(datetime.fromisoformat(
                            exp_str.replace("Z", "+00:00")).timestamp())
                        break
                    except Exception:
                        pass
            await db.execute(
                """INSERT OR IGNORE INTO wx_alerts
                   (ts, source, event_id, event_type, headline, description, expires, area)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    _now(), "nws", event_id,
                    props.get("event"),
                    props.get("headline"),
                    (props.get("description") or "")[:800],
                    expires,
                    (props.get("areaDesc") or "")[:200],
                ),
            )
            new_count += 1
        if new_count:
            await db.commit()
            logger.info(f"Stored {new_count} new NWS alert(s).")

    # ── Shared refresh helper (used by background loops, !wxrefresh, rehash) ──

    async def _refresh_all() -> str:
        """Fetch fresh forecast and alerts. Returns a short status string."""
        results = []
        try:
            await _fetch_forecast()
            results.append("forecast updated")
        except Exception as e:
            logger.error(f"Forecast fetch error: {e}")
            results.append(f"forecast failed: {e}")
        try:
            await _fetch_alerts()
            results.append("alerts updated")
        except Exception as e:
            logger.error(f"Alert fetch error: {e}")
            results.append(f"alerts failed: {e}")
        return ", ".join(results)

    # ── Auto-broadcast new alerts ─────────────────────────────────────────────

    async def _broadcast_new_alerts():
        alert_channel = _cfg().get("alert_channel", "")
        if not alert_channel:
            return
        rows = await db.fetchall(
            """SELECT id, event_type, headline, area FROM wx_alerts
               WHERE broadcast=0 AND (expires IS NULL OR expires > ?)
               ORDER BY ts ASC""",
            (_now(),),
        )
        for row in rows:
            text = f"⚠ NWS ALERT: {row['event_type']}"
            if row["headline"]:
                text += f"\n{row['headline']}"
            if row["area"]:
                text += f"\nArea: {row['area'][:100]}"
            await dispatcher.reply_queue.put({
                "target_id": None,
                "channel": alert_channel,
                "text": text,
                "part": 1, "total": 1,
            })
            await db.execute("UPDATE wx_alerts SET broadcast=1 WHERE id=?", (row["id"],))
        if rows:
            await db.commit()

    # ── Background polling loops ──────────────────────────────────────────────

    async def _alert_loop():
        await asyncio.sleep(5)
        while True:
            try:
                await _fetch_alerts()
                await _broadcast_new_alerts()
            except Exception as e:
                logger.error(f"Alert poll error: {e}")
            await asyncio.sleep(_cfg().get("poll_interval", 900))

    async def _forecast_loop():
        await asyncio.sleep(15)
        while True:
            try:
                await _fetch_forecast()
            except Exception as e:
                logger.error(f"Forecast poll error: {e}")
            await asyncio.sleep(_cfg().get("forecast_interval", 3600))

    started = {"done": False}

    async def startup_listener(msg):
        if not started["done"]:
            asyncio.create_task(_alert_loop())
            asyncio.create_task(_forecast_loop())
            started["done"] = True

    dispatcher.register_listener(startup_listener)

    # ── Rehash callback — re-fetches when zone/coords change ─────────────────

    async def on_rehash():
        logger.info("Weather rehash: re-fetching NWS data for (possibly new) zone/coords.")
        result = await _refresh_all()
        return f"[weather] {result}"

    dispatcher.register_rehash_callback(on_rehash)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_wxrefresh(msg):
        dispatcher.log_admin_attempt("!wxrefresh", msg, granted=True)
        result = await _refresh_all()
        return f"NWS refresh: {result}"

    dispatcher.register_admin_command(
        "!wxrefresh", cmd_wxrefresh,
        help_text="(Admin) Force immediate NWS data refresh",
        scope="direct", priv_floor=15, category="weather", plugin_name="weather",
    )

    async def cmd_wx(msg):
        zone     = _cfg().get("zone", "")
        lat      = _cfg().get("lat")
        lon      = _cfg().get("lon")
        zone_key = zone or (f"{lat},{lon}" if lat and lon else "")
        if not zone_key:
            return "Weather not configured. Set zone or lat/lon in config/plugins/weather.yaml"

        # Optional arg: number of forecast periods to show (default 2, max 8)
        arg = msg.arg_str.strip()
        try:
            n_periods = max(1, min(int(arg), 8)) if arg else 2
        except ValueError:
            return "Usage: !wx [periods]  — periods is a number 1-8, default 2"

        row = await db.fetchone(
            "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
            (zone_key,),
        )
        if not row:
            await _fetch_forecast()
            row = await db.fetchone(
                "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
                (zone_key,),
            )
        if not row:
            return "No forecast cached. Internet may be offline. Use !alerts for cached alerts."

        periods = json.loads(row["raw"])
        lines   = [f"Forecast ({_fmt_ts(row['ts'])})"]
        for p in periods[:n_periods]:
            name   = p.get("name", "")
            detail = p.get("shortForecast") or p.get("detailedForecast", "")
            temp   = p.get("temperature", "")
            unit   = p.get("temperatureUnit", "F")
            lines.append(f"{name}: {temp}°{unit}, {detail[:80]}")
        return "\n".join(lines)

    dispatcher.register_command(
        "!wx", cmd_wx,
        help_text="Current NWS forecast",
        usage_text="!wx [periods, default 2, max 8]",
        scope="channel", priv_floor=1, category="weather", plugin_name="weather",
    )

    async def cmd_alerts(msg):
        rows = await db.fetchall(
            """SELECT id, source, event_type, headline, area, ts, expires
               FROM wx_alerts
               WHERE expires IS NULL OR expires > ?
               ORDER BY ts DESC LIMIT 5""",
            (_now(),),
        )
        if not rows:
            zone = _cfg().get("zone", "configured area")
            return f"No active alerts for {zone}. ✓"
        lines = [f"Active alerts ({len(rows)}):"]
        for r in rows:
            src = "📻" if r["source"] == "same" else "🌐"
            exp = f" exp {_fmt_ts(r['expires'])}" if r["expires"] else ""
            lines.append(f"{src} #{r['id']} {r['event_type'] or 'Alert'}{exp}")
            if r["headline"]:
                lines.append(f"   {r['headline'][:100]}")
        lines.append("Use !alert <id> for full text.")
        return "\n".join(lines)

    dispatcher.register_command(
        "!alerts", cmd_alerts,
        help_text="Active NWS/EAS alerts",
        scope="channel", priv_floor=1, category="weather", plugin_name="weather",
    )

    async def cmd_alert(msg):
        try:
            alert_id = int(msg.arg_str.strip())
        except (ValueError, TypeError):
            return "Usage: !alert <id>"
        row = await db.fetchone("SELECT * FROM wx_alerts WHERE id=?", (alert_id,))
        if not row:
            return f"Alert #{alert_id} not found."
        src = "SDR/SAME" if row["source"] == "same" else "NWS API"
        exp = _fmt_ts(row["expires"]) if row["expires"] else "unknown"
        text = f"Alert #{row['id']} via {src}\nType: {row['event_type'] or 'N/A'}\nExpires: {exp}\n"
        if row["area"]:
            text += f"Area: {row['area'][:120]}\n"
        if row["headline"]:
            text += row["headline"][:200]
        return text

    dispatcher.register_command(
        "!alert", cmd_alert,
        help_text="Read an alert by ID",
        usage_text="!alert <id>",
        scope="direct", priv_floor=1, category="weather", plugin_name="weather",
    )
