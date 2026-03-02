"""
Plugin: Check-in / Welfare Tracking
Commands:
  !checkin [note]  — Log a welfare check-in
  !status [name]   — Last check-in for yourself or a named station
  !missing [hours] — Stations not checked in within N hours (default 24)
  !roll            — List all known stations and last check-in time
"""

__version__ = "0.3.1"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.
import time
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    sender_id   TEXT NOT NULL,
    sender_name TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkins_sender ON checkins(sender_id);
CREATE INDEX IF NOT EXISTS idx_checkins_ts     ON checkins(ts);
"""


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _fmt_age(ts):
    delta = int(time.time()) - ts
    if delta < 3600:  return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _display(name, id_):
    """'Name (hash)' when name known, else just hash."""
    if name and name != id_:
        return f"{name} ({id_})"
    return id_


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    async def cmd_checkin(msg):
        note = msg.arg_str.strip() if msg.arg_str else ""
        await db.execute(
            "INSERT INTO checkins (ts, sender_id, sender_name, note) VALUES (?,?,?,?)",
            (msg.ts, msg.sender_id, msg.sender_name, note),
        )
        await db.commit()
        note_part = f' — "{note}"' if note else ""
        return f"Check-in logged for {_display(msg.sender_name, msg.sender_id)}{note_part} at {_fmt_ts(msg.ts)}"

    dispatcher.register_command(
        "!checkin", cmd_checkin,
        help_text="Log a welfare check-in",
        usage_text="!checkin [note]",
        scope="direct", priv_floor=PRIV_DEFAULT, category="welfare", plugin_name="checkin",
    )

    async def cmd_status(msg):
        target = msg.arg_str.strip()
        if target:
            row = await db.fetchone(
                """SELECT sender_name, sender_id, ts, note FROM checkins
                   WHERE sender_id LIKE ? OR sender_name LIKE ?
                   ORDER BY ts DESC LIMIT 1""",
                (f"%{target}%", f"%{target}%"),
            )
            if not row:
                return f"No check-ins found for '{target}'."
            note = f' — "{row["note"]}"' if row["note"] else ""
            return f"{_display(row['sender_name'], row['sender_id'])}: last seen {_fmt_age(row['ts'])} ({_fmt_ts(row['ts'])}){note}"
        else:
            row = await db.fetchone(
                "SELECT ts, note FROM checkins WHERE sender_id=? ORDER BY ts DESC LIMIT 1",
                (msg.sender_id,),
            )
            if not row:
                return "No check-ins logged for you yet. Use !checkin."
            note = f' — "{row["note"]}"' if row["note"] else ""
            return f"Your last check-in: {_fmt_age(row['ts'])} ({_fmt_ts(row['ts'])}){note}"

    dispatcher.register_command(
        "!status", cmd_status,
        help_text="Show welfare status for a station",
        usage_text="!status [name or ID]",
        scope="direct", priv_floor=PRIV_DEFAULT, category="welfare", plugin_name="checkin",
    )

    async def cmd_missing(msg):
        cfg = config.plugin("checkin")
        try:
            hours = int(msg.arg_str.strip()) if msg.arg_str.strip() else cfg.get("default_missing_hours", 24)
        except ValueError:
            return "Usage: !missing [hours]"
        cutoff       = int(time.time()) - (hours * 3600)
        all_stations = await db.fetchall("SELECT DISTINCT sender_id, sender_name FROM checkins")
        missing = []
        for s in all_stations:
            row = await db.fetchone(
                "SELECT ts FROM checkins WHERE sender_id=? ORDER BY ts DESC LIMIT 1",
                (s["sender_id"],),
            )
            if row and row["ts"] < cutoff:
                missing.append(f"{_display(s['sender_name'], s['sender_id'])} ({_fmt_age(row['ts'])})")
        if not missing:
            return f"All stations checked in within {hours}h."
        return f"Not checked in ({hours}h):\n" + "\n".join(f"  {m}" for m in missing)

    dispatcher.register_command(
        "!missing", cmd_missing,
        help_text="List stations not checked in",
        usage_text="!missing [hours, default 24]",
        scope="channel", priv_floor=PRIV_DEFAULT, category="welfare", plugin_name="checkin",
    )

    async def cmd_roll(msg):
        cfg      = config.plugin("checkin")
        max_roll = cfg.get("max_roll", 20)
        stations = await db.fetchall(
            """SELECT sender_name, sender_id, MAX(ts) as last_ts
               FROM checkins GROUP BY sender_id ORDER BY last_ts DESC LIMIT ?""",
            (max_roll,),
        )
        if not stations:
            return "No check-ins recorded yet."
        lines = [f"Station roll ({len(stations)}):"]
        for s in stations:
            lines.append(f"  {_display(s['sender_name'], s['sender_id'])}: {_fmt_age(s['last_ts'])}")
        return "\n".join(lines)

    dispatcher.register_command(
        "!roll", cmd_roll,
        help_text="List all stations and last check-in time",
        scope="direct", priv_floor=PRIV_DEFAULT, category="welfare", plugin_name="checkin",
    )
