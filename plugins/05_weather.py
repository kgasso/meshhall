"""
Plugin: Weather / NWS
Commands:
  !wx [periods]    — Forecast for the bot's home ZIP (or user's !setloc ZIP)
  !wx <zip>        — NWS forecast for a specific US ZIP code
  !alerts          — Active NWS alerts for the home ZIP (or user's !setloc ZIP)
  !alerts <zip>    — Active NWS alerts for a specific US ZIP code
  !alert <id>      — Read full alert text
  !setloc <zip>    — Save your home ZIP code for personalised !wx and !alerts
  !setloc clear    — Remove your saved home ZIP
  !wxrefresh       — (Admin) Force immediate NWS data refresh

Alert sources:
  1. NWS API (api.weather.gov) — polled on schedule and on !rehash/!wxrefresh
  2. SDR SAME/EAS — written to DB by meshhall-same (separate repository),
     provides offline-capable alerts via RTL-SDR dongle

Location:
  All location lookups (home zone, per-user !setloc, !wx <zip>, !alerts <zip>)
  resolve through the same ZIP→lat/lon→NWS path using data/zip_code_database.csv.
  Set home_zip in weather.yaml. Run !rehash after changing it.

ZIP lookup:
  US ZIP → (lat, lon, city, state) resolved from a CSV file (load-once dict).
  Default source: http://uszipcodelist.com/zip_code_database.csv
  Default path:   data/zip_code_database.csv
  Both the file path and column header names are configurable in weather.yaml
  under zip_csv_path and zip_columns. See weather.yaml for the column map.

User location:
  Users can store a home ZIP with !setloc. When set, bare !wx and !alerts use
  that ZIP instead of the bot's configured home ZIP. Stored in users.home_zip
  (database migration adds this column automatically on first run).

Config: config/plugins/weather.yaml
"""

__version__ = "0.5.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import asyncio
import csv
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
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
NWS_ALERTS_POINT = "https://api.weather.gov/alerts/active?point={lat},{lon}"
NWS_POINT        = "https://api.weather.gov/points/{lat},{lon}"
NWS_FORECAST     = "https://api.weather.gov/gridpoints/{office}/{x},{y}/forecast"

# Default per-zip forecast cache TTL in seconds (30 minutes).
# Override with zip_cache_ttl in weather.yaml.
ZIP_CACHE_TTL = 1800

# Default path to the ZIP centroid CSV, relative to the project root.
# Override with zip_csv_path in weather.yaml.
ZIP_CSV_DEFAULT_PATH = "data/zip_code_database.csv"

# Default column name mapping: internal field -> CSV header.
# Override any or all keys with zip_columns in weather.yaml.
ZIP_COLUMNS_DEFAULT = {
    "zip":       "zip",
    "latitude":  "latitude",
    "longitude": "longitude",
    "city":      "primary_city",
    "state":     "state",
}


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _now() -> int:
    return int(time.time())


def _load_zip_table(csv_path: str, col_map: dict) -> dict:
    """
    Load ZIP -> (lat, lon, city, state) mapping from a CSV file into memory.
    Returns an empty dict and logs a warning if the file is missing or malformed.

    csv_path : path to the CSV file, relative to the project root.
    col_map  : dict mapping internal keys (zip, latitude, longitude, city,
               state) to the actual column headers present in the CSV.

    Decommissioned ZIPs are skipped automatically if the dataset includes a
    'decommissioned' column with value '1'.

    Source (default dataset): http://uszipcodelist.com/zip_code_database.csv
    """
    path = Path(csv_path)
    if not path.exists():
        logger.warning(
            f"ZIP data file not found at {path}. "
            "Download from http://uszipcodelist.com/zip_code_database.csv and save as "
            f"{csv_path}. '!wx <zip>', '!alerts <zip>', and !setloc will be unavailable."
        )
        return {}
    col_zip   = col_map.get("zip",       "zip")
    col_lat   = col_map.get("latitude",  "latitude")
    col_lon   = col_map.get("longitude", "longitude")
    col_city  = col_map.get("city",      "primary_city")
    col_state = col_map.get("state",     "state")
    table    = {}
    skipped  = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("decommissioned", "0") == "1":
                    skipped += 1
                    continue
                z = row.get(col_zip, "").strip().zfill(5)
                try:
                    table[z] = (
                        float(row[col_lat]),
                        float(row[col_lon]),
                        row.get(col_city, "").strip(),
                        row.get(col_state, "").strip(),
                    )
                except (KeyError, ValueError):
                    continue
        skip_note = f", {skipped} decommissioned skipped" if skipped else ""
        logger.info(f"Loaded {len(table):,} ZIP centroids from {path}{skip_note}")
    except Exception as e:
        logger.error(f"Failed to load ZIP data from {path}: {e}")
    return table


def _hash_file(path: Path) -> Optional[str]:
    """SHA-256 of a file in streaming 64 KB chunks. Returns None if unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    # Load ZIP table once at plugin load time — read-only dict, fits in memory.
    # The file hash is stored so !rehash can detect changes without re-parsing.
    _zip_csv_path  = config.plugin("weather").get("zip_csv_path", ZIP_CSV_DEFAULT_PATH)
    _zip_col_map   = {**ZIP_COLUMNS_DEFAULT,
                      **config.plugin("weather").get("zip_columns", {})}
    _zip_table:     dict          = _load_zip_table(_zip_csv_path, _zip_col_map)
    _zip_file_hash: Optional[str] = _hash_file(Path(_zip_csv_path))

    # All config read lazily through _cfg() so !rehash takes immediate effect.
    def _cfg():
        return config.plugin("weather")

    # ── ZIP resolution helpers ────────────────────────────────────────────────

    def _resolve_zip(zipcode: str) -> Optional[tuple]:
        """Return (lat, lon, city, state) for a ZIP string, or None if not in table."""
        return _zip_table.get(zipcode.strip().zfill(5))

    def _zip_label(zipcode: str, coords: tuple) -> str:
        """Format a human-readable location label from ZIP lookup result."""
        _, _, city, state = coords
        if city and state:
            return f"{city}, {state}"
        if city:
            return city
        return f"ZIP {zipcode}"

    def _zip_unavailable_msg() -> str:
        return (
            "ZIP lookup unavailable — data/zip_code_database.csv not found. "
            "See plugins/05_weather.py for setup instructions."
        )

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

    async def _fetch_forecast_for(lat: float, lon: float, zone_key: str) -> bool:
        """
        Fetch and store a forecast for the given lat/lon under zone_key.
        Returns True on success, False on any NWS failure.
        On failure the existing cache row, if any, remains intact for fallback.
        """
        data = await _get(NWS_POINT.format(lat=lat, lon=lon))
        if not data:
            return False
        props  = data.get("properties", {})
        office = props.get("gridId")
        x, y   = props.get("gridX"), props.get("gridY")
        if not all([office, x, y]):
            logger.warning(f"NWS /points did not return grid info for {zone_key}.")
            return False
        forecast = await _get(NWS_FORECAST.format(office=office, x=x, y=y))
        if not forecast:
            return False
        periods = forecast.get("properties", {}).get("periods", [])[:4]
        await db.execute(
            "INSERT INTO wx_forecast (ts, zone, source, raw) VALUES (?,?,?,?)",
            (_now(), zone_key, "nws", json.dumps(periods)),
        )
        await db.commit()
        logger.info(f"NWS forecast updated for zone {zone_key}.")
        return True

    async def _fetch_forecast():
        """Fetch forecast for the bot's configured home ZIP."""
        home_zip = _cfg().get("home_zip", "").strip().zfill(5) if _cfg().get("home_zip") else ""
        if not home_zip:
            logger.warning("Weather: home_zip not configured, skipping forecast fetch.")
            return
        coords = _resolve_zip(home_zip)
        if not coords:
            logger.warning(f"Weather: home_zip {home_zip} not found in ZIP table.")
            return
        lat, lon, _, _ = coords
        await _fetch_forecast_for(lat, lon, f"zip:{home_zip}")

    # ── Alerts fetch ──────────────────────────────────────────────────────────

    async def _fetch_alerts_for_point(lat: float, lon: float) -> Optional[list]:
        """
        Fetch active NWS alerts for a lat/lon point.
        Returns a list of feature dicts, or None on failure.
        """
        data = await _get(NWS_ALERTS_POINT.format(lat=lat, lon=lon))
        if not data:
            return None
        return data.get("features", [])

    async def _fetch_alerts():
        """Fetch and store alerts for the bot's configured home ZIP."""
        home_zip = _cfg().get("home_zip", "").strip().zfill(5) if _cfg().get("home_zip") else ""
        if not home_zip:
            logger.warning("Weather: home_zip not configured, skipping alert fetch.")
            return
        coords = _resolve_zip(home_zip)
        if not coords:
            logger.warning(f"Weather: home_zip {home_zip} not found in ZIP table.")
            return
        lat, lon, _, _ = coords
        features = await _fetch_alerts_for_point(lat, lon)
        if features is None:
            return
        await _store_alert_features(features)
    async def _store_alert_features(features: list) -> int:
        """Persist a list of NWS feature dicts. Returns count of new alerts stored."""
        new_count = 0
        for f in features:
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
        return new_count

    # ── Shared refresh helper ─────────────────────────────────────────────────

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

    # ── Forecast response builder ─────────────────────────────────────────────

    def _build_forecast_response(row, n_periods: int, stale: bool = False,
                                  location: str = "") -> str:
        """
        Format a wx_forecast DB row into a reply string.
        stale=True adds a '[cached MM-DD HH:MMz]' note when NWS was unreachable.
        location, if provided, is shown on the header line.
        """
        periods   = json.loads(row["raw"])
        age_note  = f" [cached {_fmt_ts(row['ts'])}]" if stale else f" ({_fmt_ts(row['ts'])})"
        loc_note  = f" — {location}" if location else ""
        lines     = [f"Forecast{loc_note}{age_note}"]
        for p in periods[:n_periods]:
            name   = p.get("name", "")
            detail = p.get("shortForecast") or p.get("detailedForecast", "")
            temp   = p.get("temperature", "")
            unit   = p.get("temperatureUnit", "F")
            lines.append(f"{name}: {temp}°{unit}, {detail[:80]}")
        return "\n".join(lines)

    # ── Alert response builder ────────────────────────────────────────────────

    def _build_alerts_response(rows, location_label: str) -> str:
        """Format a list of wx_alerts DB rows into a reply string."""
        if not rows:
            return f"No active alerts for {location_label}. ✓"
        lines = [f"Active alerts ({len(rows)}):"]
        for r in rows:
            src = "📻" if r["source"] == "same" else "🌐"
            exp = f" exp {_fmt_ts(r['expires'])}" if r["expires"] else ""
            lines.append(f"{src} #{r['id']} {r['event_type'] or 'Alert'}{exp}")
            if r["headline"]:
                lines.append(f"   {r['headline'][:100]}")
        lines.append("Use !alert <id> for full text.")
        return "\n".join(lines)

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

    # ── Rehash callback ───────────────────────────────────────────────────────

    async def on_rehash():
        nonlocal _zip_table, _zip_file_hash
        logger.info("Weather rehash: re-fetching NWS data for home_zip.")

        # ZIP table — reload only if the file has changed since last load.
        zip_path    = config.plugin("weather").get("zip_csv_path", ZIP_CSV_DEFAULT_PATH)
        current_hash = _hash_file(Path(zip_path))
        if current_hash is None:
            zip_note = "ZIP file not found — table unchanged"
        elif current_hash == _zip_file_hash:
            zip_note = "ZIP data unchanged"
        else:
            old_count  = len(_zip_table)
            new_col_map = {**ZIP_COLUMNS_DEFAULT,
                           **config.plugin("weather").get("zip_columns", {})}
            _zip_table    = _load_zip_table(zip_path, new_col_map)
            _zip_file_hash = current_hash
            zip_note = f"ZIP reloaded ({old_count:,} → {len(_zip_table):,} entries)"
            logger.info(f"Weather: {zip_note}")

        nws_result = await _refresh_all()
        return f"[weather] {nws_result}; {zip_note}"

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

    # ── !setloc ───────────────────────────────────────────────────────────────

    async def cmd_setloc(msg):
        arg = msg.arg_str.strip()

        if arg.lower() == "clear":
            ok = await db.set_home_zip(msg.sender_id, None)
            if not ok:
                return "Could not clear location — user record not found."
            return "Your home ZIP has been cleared. !wx and !alerts will use the local area."

        if not arg:
            current = await db.get_home_zip(msg.sender_id)
            if current:
                return f"Your home ZIP is {current}. Use !setloc <zip> to change or !setloc clear to remove."
            return "No home ZIP set. Use !setloc <zip> to set one."

        if not (arg.isdigit() and len(arg) == 5):
            return "Usage: !setloc <5-digit ZIP>  |  !setloc clear"

        zipcode = arg.zfill(5)

        if not _zip_table:
            return _zip_unavailable_msg()

        if not _resolve_zip(zipcode):
            return f"ZIP {zipcode} not found in the database. Check the ZIP and try again."

        ok = await db.set_home_zip(msg.sender_id, zipcode)
        if not ok:
            return "Could not save location — user record not found."
        return f"Home ZIP set to {zipcode}. !wx and !alerts will now use your location."

    dispatcher.register_command(
        "!setloc", cmd_setloc,
        help_text="Save your home ZIP for personalised !wx and !alerts",
        usage_text=(
            "!setloc <zip>   — set your home ZIP code\n"
            "!setloc         — show your current home ZIP\n"
            "!setloc clear   — remove your home ZIP"
        ),
        scope="direct", priv_floor=1, category="weather", plugin_name="weather",
    )

    # ── !wx ───────────────────────────────────────────────────────────────────

    async def cmd_wx(msg):
        arg = msg.arg_str.strip()

        # ── ZIP code path — explicit arg or user's saved home ZIP ─────────────
        # Determine the target ZIP: explicit arg takes priority, then setloc.
        target_zip = None
        if arg and arg.isdigit() and len(arg) == 5:
            target_zip = arg.zfill(5)
        elif not arg:
            target_zip = await db.get_home_zip(msg.sender_id)

        if target_zip:
            if not _zip_table:
                return _zip_unavailable_msg()

            coords = _resolve_zip(target_zip)
            if not coords:
                if arg:
                    return f"ZIP {target_zip} not found. Try !wx for the local forecast."
                else:
                    return (
                        f"Your saved ZIP {target_zip} wasn't found in the database. "
                        "Use !setloc clear to reset or !setloc <zip> to update."
                    )

            lat, lon, _, _ = coords
            zone_key = f"zip:{target_zip}"
            ttl      = _cfg().get("zip_cache_ttl", ZIP_CACHE_TTL)
            location = _zip_label(target_zip, coords)

            row = await db.fetchone(
                "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
                (zone_key,),
            )
            cache_fresh = row and (_now() - row["ts"]) < ttl

            if not cache_fresh:
                success = await _fetch_forecast_for(lat, lon, zone_key)
                if success:
                    row = await db.fetchone(
                        "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
                        (zone_key,),
                    )
                elif not row:
                    return (
                        f"NWS is unreachable and no cached forecast exists for {location}. "
                        "Try again later or use !wx for the local forecast."
                    )

            stale = not cache_fresh and row and not (
                await db.fetchone(
                    "SELECT id FROM wx_forecast WHERE zone=? AND ts>?",
                    (zone_key, _now() - ttl),
                )
            )
            return _build_forecast_response(row, n_periods=2, stale=stale,
                                            location=location)

        # ── Home ZIP path ─────────────────────────────────────────────────────
        home_zip = _cfg().get("home_zip", "").strip().zfill(5) if _cfg().get("home_zip") else ""
        if not home_zip:
            return "Weather not configured. Set home_zip in config/plugins/weather.yaml"

        if not _zip_table:
            return _zip_unavailable_msg()

        coords = _resolve_zip(home_zip)
        if not coords:
            return (
                f"home_zip {home_zip} not found in ZIP table. "
                "Check weather.yaml and !rehash."
            )

        lat, lon, _, _ = coords
        zone_key = f"zip:{home_zip}"
        location = _zip_label(home_zip, coords)

        try:
            n_periods = max(1, min(int(arg), 8)) if arg else 2
        except ValueError:
            return "Usage: !wx [periods|zip]  — periods 1-8 or a 5-digit US ZIP code"

        row = await db.fetchone(
            "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
            (zone_key,),
        )
        if not row:
            await _fetch_forecast_for(lat, lon, zone_key)
            row = await db.fetchone(
                "SELECT ts, raw FROM wx_forecast WHERE zone=? ORDER BY ts DESC LIMIT 1",
                (zone_key,),
            )
        if not row:
            return "NWS is unreachable and no cached forecast exists. Use !alerts for cached alerts."

        forecast_interval = _cfg().get("forecast_interval", 3600)
        stale = (_now() - row["ts"]) > int(forecast_interval * 1.5)
        return _build_forecast_response(row, n_periods, stale=stale, location=location)

    dispatcher.register_command(
        "!wx", cmd_wx,
        help_text="NWS forecast for your area, a ZIP code, or the local area",
        usage_text=(
            "!wx             — forecast for your !setloc ZIP (or local area if not set)\n"
            "!wx [periods]   — 1-8 forecast periods for local area (default 2)\n"
            "!wx <zip>       — forecast for any US ZIP code"
        ),
        scope="direct", priv_floor=1, category="weather", plugin_name="weather",
        allow_channel=True)

    # ── !alerts ───────────────────────────────────────────────────────────────

    async def cmd_alerts(msg):
        arg = msg.arg_str.strip()

        # Determine target: explicit ZIP arg, then user's setloc, then home zone.
        target_zip = None
        if arg and arg.isdigit() and len(arg) == 5:
            target_zip = arg.zfill(5)
        elif not arg:
            target_zip = await db.get_home_zip(msg.sender_id)

        if target_zip:
            if not _zip_table:
                return _zip_unavailable_msg()

            coords = _resolve_zip(target_zip)
            if not coords:
                if arg:
                    return f"ZIP {target_zip} not found."
                else:
                    return (
                        f"Your saved ZIP {target_zip} wasn't found in the database. "
                        "Use !setloc clear to reset or !setloc <zip> to update."
                    )

            lat, lon, _, _ = coords
            label    = _zip_label(target_zip, coords)

            # Fetch live alerts for this point from NWS.
            features = await _fetch_alerts_for_point(lat, lon)
            if features is None:
                return "NWS is unreachable. Try again later or use !alerts for cached home-zone alerts."

            # Persist first (dedup-safe) so every alert gets a real DB id,
            # then query back by event_id to build the response. This ensures
            # !alert <id> always works for alerts discovered via !alerts <zip>.
            await _store_alert_features(features)

            # Collect event_ids from the live response to query their DB rows.
            event_ids = [
                (f.get("properties", {}).get("id") or f.get("id"))
                for f in features
                if (f.get("properties", {}).get("id") or f.get("id"))
            ]
            if not event_ids:
                return f"No active alerts for {label}. \u2713"

            placeholders = ",".join("?" * len(event_ids))
            rows = await db.fetchall(
                f"""SELECT id, source, event_type, headline, area, expires
                    FROM wx_alerts
                    WHERE event_id IN ({placeholders})
                      AND (expires IS NULL OR expires > ?)
                    ORDER BY ts DESC LIMIT 5""",
                (*event_ids, _now()),
            )
            return _build_alerts_response(rows, label)

        # ── Home ZIP path — served from DB cache ──────────────────────────────
        home_zip = _cfg().get("home_zip", "").strip().zfill(5) if _cfg().get("home_zip") else ""
        if home_zip and _zip_table:
            coords = _resolve_zip(home_zip)
            label  = _zip_label(home_zip, coords) if coords else f"ZIP {home_zip}"
        else:
            label  = "configured area"
        rows = await db.fetchall(
            """SELECT id, source, event_type, headline, area, ts, expires
               FROM wx_alerts
               WHERE expires IS NULL OR expires > ?
               ORDER BY ts DESC LIMIT 5""",
            (_now(),),
        )
        return _build_alerts_response(rows, label)

    dispatcher.register_command(
        "!alerts", cmd_alerts,
        help_text="Active NWS/EAS alerts for your area or a ZIP code",
        usage_text=(
            "!alerts         — alerts for your !setloc ZIP (or home zone if not set)\n"
            "!alerts <zip>   — alerts for any US ZIP code"
        ),
        scope="direct", priv_floor=1, category="weather", plugin_name="weather",
        allow_channel=True)

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
