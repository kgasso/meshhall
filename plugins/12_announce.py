"""
Plugin: Scheduled Announcements
Commands:
  !announce                          -- list subcommands
  !announce list                     -- list all announcements
  !announce show <slug>              -- show announcement details
  !announce create <slug> <channel> <schedule> <text>
                                     -- create an announcement (admin)
  !announce delete <slug>            -- delete an announcement (admin)

Schedule format (same as nets):
  daily <HH:MM>                      e.g. daily 07:00
  weekly <day> <HH:MM>               e.g. weekly tuesday 19:00
  monthly <Nth> <day> <HH:MM>        e.g. monthly 3rd tuesday 08:00
  once <YYYY-MM-DD> <HH:MM>          one-shot at a specific date/time

Recurrence:
  Announcements with daily/weekly/monthly schedules fire on every matching
  occurrence (recurring=1). One-shot announcements (recurring=0) fire once
  at the specified time and are then automatically deactivated.

Timezone defaults to bot.timezone from config.yaml. Channel must be a
channel name the bot is configured to respond on.

Config: none -- all settings stored in DB via !announce create.
"""

__version__ = "0.1.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.database import PRIV_ADMIN, PRIV_DEFAULT

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    channel     TEXT NOT NULL,
    channel_idx INTEGER,
    text        TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    cron_expr   TEXT,
    once_ts     INTEGER,
    recurring   INTEGER NOT NULL DEFAULT 1,
    timezone    TEXT NOT NULL DEFAULT 'UTC',
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_ts  INTEGER NOT NULL,
    last_sent_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_announcements_slug ON announcements(slug);
"""


# -- Schedule parsing ----------------------------------------------------------
# Reuses the same day/ordinal tables as 02_nets.py

# croniter (and standard cron) use 0=Sunday, 1=Monday, ..., 6=Saturday.
_DAYS = {
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_ORDINALS = {
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}


def _parse_time(t: str):
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time {t!r} -- use HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time {t!r}")
    return h, m


def _parse_schedule(text: str) -> dict:
    """
    Parse a schedule string into a dict with keys:
      cron_expr   -- croniter expression (None for once)
      once_ts     -- epoch timestamp (None for recurring)
      recurring   -- bool
      human       -- human-readable summary

    Accepted formats:
      daily <HH:MM>
      weekly <day> <HH:MM>
      monthly <Nth> <day> <HH:MM>
      once <YYYY-MM-DD> <HH:MM>
    """
    tokens = text.strip().lower().split()
    if not tokens:
        raise ValueError("Schedule cannot be empty.")

    kind = tokens[0]

    if kind == "daily":
        if len(tokens) != 2:
            raise ValueError("Usage: daily HH:MM")
        h, m = _parse_time(tokens[1])
        return {
            "cron_expr": f"{m} {h} * * *",
            "once_ts":   None,
            "recurring": True,
            "human":     f"Daily at {tokens[1]}",
        }

    if kind == "weekly":
        if len(tokens) != 3:
            raise ValueError("Usage: weekly <day> HH:MM")
        day_str, time_str = tokens[1], tokens[2]
        if day_str not in _DAYS:
            raise ValueError(f"Unknown day {day_str!r}.")
        h, m = _parse_time(time_str)
        dow  = _DAYS[day_str]
        return {
            "cron_expr": f"{m} {h} * * {dow}",
            "once_ts":   None,
            "recurring": True,
            "human":     f"Weekly on {day_str.capitalize()} at {time_str}",
        }

    if kind == "monthly":
        if len(tokens) != 4:
            raise ValueError("Usage: monthly <Nth> <day> HH:MM")
        ord_str, day_str, time_str = tokens[1], tokens[2], tokens[3]
        if ord_str not in _ORDINALS:
            raise ValueError(f"Unknown ordinal {ord_str!r}. Use 1st, 2nd, 3rd, 4th, 5th.")
        if day_str not in _DAYS:
            raise ValueError(f"Unknown day {day_str!r}.")
        n   = _ORDINALS[ord_str]
        dow = _DAYS[day_str]
        h, m = _parse_time(time_str)
        return {
            "cron_expr": f"{m} {h} * * {dow}#{n}",
            "once_ts":   None,
            "recurring": True,
            "human":     f"Monthly on the {ord_str} {day_str.capitalize()} at {time_str}",
        }

    if kind == "once":
        if len(tokens) != 3:
            raise ValueError("Usage: once YYYY-MM-DD HH:MM")
        try:
            dt = datetime.strptime(f"{tokens[1]} {tokens[2]}", "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("Usage: once YYYY-MM-DD HH:MM  e.g. once 2026-04-01 09:00")
        return {
            "cron_expr": None,
            "once_ts":   int(dt.replace(tzinfo=timezone.utc).timestamp()),
            "recurring": False,
            "human":     f"Once on {tokens[1]} at {tokens[2]}",
        }

    raise ValueError(
        f"Unknown schedule type {kind!r}. Use: daily, weekly, monthly, or once."
    )


def _tz(tz_str: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _next_occurrence(cron_expr: str, tz_str: str) -> Optional[datetime]:
    try:
        from croniter import croniter
        from datetime import timedelta
        tz  = _tz(tz_str)
        # Start croniter 65 seconds in the past so an occurrence that is
        # imminent (within the next ~60s) is returned as the next hit rather
        # than skipped to the following period.
        start = datetime.now(tz) - timedelta(seconds=65)
        cron  = croniter(cron_expr, start)
        return cron.get_next(datetime).replace(tzinfo=tz)
    except Exception as e:
        logger.warning(f"croniter error for {cron_expr!r}: {e}")
        return None


def _fmt_next(ann: dict) -> str:
    if ann["cron_expr"]:
        dt = _next_occurrence(ann["cron_expr"], ann["timezone"])
        if dt:
            return dt.strftime("%a %Y-%m-%d %H:%M") + f" {ann['timezone']}"
        return "unknown (croniter not installed)"
    if ann["once_ts"]:
        return datetime.fromtimestamp(ann["once_ts"], tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%Mz"
        )
    return "n/a"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _now() -> int:
    return int(time.time())


# -- Plugin setup --------------------------------------------------------------

def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    def _default_tz() -> str:
        return config.get("bot.timezone", "UTC")

    # -- Subcommand handlers ---------------------------------------------------

    async def do_list(msg, args=""):
        rows = await db.fetchall(
            "SELECT slug, channel, schedule, recurring, active FROM announcements ORDER BY slug"
        )
        if not rows:
            cc = dispatcher.command_char
            return f"No announcements configured. Use {cc}announce create to add one."
        lines = ["Announcements:"]
        for r in rows:
            rec    = "recurring" if r["recurring"] else "one-shot"
            status = "active" if r["active"] else "inactive"
            lines.append(f"  {r['slug']}: #{r['channel']} | {r['schedule']} ({rec}, {status})")
        return "\n".join(lines)

    async def do_show(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !announce show <slug>"
        row = await db.fetchone("SELECT * FROM announcements WHERE slug=?", (slug,))
        if not row:
            return f"Announcement '{slug}' not found."
        lines = [
            f"Announcement: {row['slug']}",
            f"Channel:   #{row['channel']}",
            f"Schedule:  {row['schedule']}",
            f"Recurring: {'yes' if row['recurring'] else 'no (one-shot)'}",
            f"Active:    {'yes' if row['active'] else 'no'}",
            f"Next:      {_fmt_next(dict(row))}",
            f"Text:      {row['text']}",
        ]
        if row["last_sent_ts"]:
            lines.append(f"Last sent: {_fmt_ts(row['last_sent_ts'])}")
        return "\n".join(lines)

    async def do_create(msg, args=""):
        """
        !announce create <slug> <channel> <schedule...> -- <text>
        Schedule and text are separated by ' -- '.
        Examples:
          !announce create morning-wx general daily 07:00 -- Good morning. Check !wx.
          !announce create net-reminder ares weekly tuesday 18:45 -- Net starts in 15min.
          !announce create drill-notice ares once 2026-06-01 09:00 -- ARES drill today at 09:00.
        """
        cc = dispatcher.command_char
        if " -- " not in args:
            return (
                f"Usage: {cc}announce create <slug> <channel> <schedule> -- <text>\n"
                f"Schedule: daily HH:MM | weekly <day> HH:MM | monthly <Nth> <day> HH:MM | once YYYY-MM-DD HH:MM\n"
                f"Example: {cc}announce create morning-wx general daily 07:00 -- Good morning. Check {cc}wx."
            )

        left, text = args.split(" -- ", 1)
        text = text.strip()
        if not text:
            return "Announcement text cannot be empty."

        parts = left.strip().split(None, 2)
        if len(parts) < 3:
            return f"Usage: {cc}announce create <slug> <channel> <schedule> -- <text>"

        slug, channel, schedule_str = parts[0].lower(), parts[1], parts[2]

        if not SLUG_RE.match(slug):
            return "Invalid slug -- use lowercase letters, numbers, and hyphens only."
        if len(slug) > 32:
            return "Slug too long -- max 32 characters."

        existing = await db.fetchone("SELECT id FROM announcements WHERE slug=?", (slug,))
        if existing:
            return f"An announcement with slug '{slug}' already exists."

        # Validate channel against _channels table via dispatcher provider
        ch_row = await dispatcher.get_channel_by_name(channel)
        if ch_row is None:
            return (
                f"Unknown channel '{channel}' -- not found in channel table.\n"
                f"Run {cc}channel sync to enumerate channels from radio, "
                f"then {cc}channel list to see available names."
            )
        if ch_row.get("disabled_at"):
            return (
                f"Channel '{channel}' is safety-disabled and cannot receive announcements.\n"
                f"Re-enable it with {cc}channel set {ch_row['channel_idx']} on first."
            )
        if not ch_row.get("respond"):
            return (
                f"Channel '{channel}' has respond=off and cannot receive announcements.\n"
                f"Enable it with {cc}channel set {ch_row['channel_idx']} on first."
            )
        channel_idx = ch_row["channel_idx"]

        try:
            sched = _parse_schedule(schedule_str)
        except ValueError as e:
            return f"Schedule error: {e}"

        dispatcher.log_admin_attempt("!announce create", msg, granted=True,
                                     reason=f"slug={slug} channel={channel} idx={channel_idx} schedule={sched['human']}")

        await db.execute(
            """INSERT INTO announcements
               (slug, channel, channel_idx, text, schedule, cron_expr, once_ts, recurring,
                timezone, active, created_by, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (slug, channel, channel_idx, text, sched["human"], sched["cron_expr"],
             sched["once_ts"], 1 if sched["recurring"] else 0,
             _default_tz(), msg.sender_id, _now()),
        )
        await db.commit()

        rec_note = "recurring" if sched["recurring"] else "one-shot"
        return (
            f"Announcement '{slug}' created ({rec_note}).\n"
            f"Schedule: {sched['human']}\n"
            f"Channel:  #{channel} (slot {channel_idx})\n"
            f"Next:     {_fmt_next({'cron_expr': sched['cron_expr'], 'once_ts': sched['once_ts'], 'timezone': _default_tz()})}"
        )

    async def do_delete(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !announce delete <slug>"
        row = await db.fetchone("SELECT id FROM announcements WHERE slug=?", (slug,))
        if not row:
            return f"Announcement '{slug}' not found."
        dispatcher.log_admin_attempt("!announce delete", msg, granted=True,
                                     reason=f"deleting announcement '{slug}'")
        await db.execute("DELETE FROM announcements WHERE slug=?", (slug,))
        await db.commit()
        return f"Announcement '{slug}' deleted."

    _SUBCOMMANDS = {
        "list":   do_list,
        "show":   do_show,
        "create": do_create,
        "delete": do_delete,
    }
    _ADMIN_SUBS = {"create", "delete"}

    # -- Subcommand dispatcher -------------------------------------------------

    async def cmd_announce(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc        = dispatcher.command_char
            privilege = await db.get_privilege(msg.sender_id)
            lines = [
                f"Announcement commands:",
                f"  {cc}announce list             -- list all announcements",
                f"  {cc}announce show <slug>      -- show details",
            ]
            if privilege >= PRIV_ADMIN:
                lines += [
                    f"  {cc}announce create <slug> <channel> <schedule> -- <text>",
                    f"  {cc}announce delete <slug>",
                    f"  Schedules: daily HH:MM | weekly <day> HH:MM | monthly <Nth> <day> HH:MM | once YYYY-MM-DD HH:MM",
                ]
            return "\n".join(lines)

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}announce for the list."

        if sub in _ADMIN_SUBS:
            privilege = await db.get_privilege(msg.sender_id)
            if privilege < PRIV_ADMIN:
                return f"Access denied. !announce {sub} requires admin privilege."

        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_admin_command(
        "!announce", cmd_announce,
        help_text="Scheduled channel announcements -- create, list, and delete",
        usage_text=(
            "!announce list\n"
            "!announce show <slug>\n"
            "!announce create <slug> <channel> <schedule> -- <text>\n"
            "!announce delete <slug>\n"
            "Schedules: daily HH:MM | weekly <day> HH:MM | monthly <Nth> <day> HH:MM | once YYYY-MM-DD HH:MM"
        ),
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="admin", plugin_name="announce",
    )

    # -- Background fire loop --------------------------------------------------

    async def _announce_loop():
        await asyncio.sleep(15)  # let bot fully connect before first check
        while True:
            try:
                await _tick_announcements()
            except Exception as e:
                logger.error(f"Announce loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _tick_announcements():
        now  = _now()
        rows = await db.fetchall(
            "SELECT * FROM announcements WHERE active=1"
        )
        for ann in rows:
            ann = dict(ann)
            should_fire = False

            if ann["recurring"] and ann["cron_expr"]:
                # Recurring -- fire if the previous cron occurrence is within
                # the last 2 minutes and we haven't fired since then.
                try:
                    from croniter import croniter
                    tz      = _tz(ann["timezone"])
                    now_dt  = datetime.now(tz)
                    cron_it = croniter(ann["cron_expr"], now_dt)
                    prev_dt = cron_it.get_prev(datetime).replace(tzinfo=tz)
                    prev_ts = int(prev_dt.timestamp())
                    last    = ann["last_sent_ts"] or 0
                    if now - prev_ts <= 120 and last < prev_ts:
                        should_fire = True
                except Exception as e:
                    logger.warning(f"Announce cron check failed for '{ann['slug']}': {e}")

            elif not ann["recurring"] and ann["once_ts"]:
                # One-shot -- fire if within 2-minute window and not yet sent
                last = ann["last_sent_ts"] or 0
                if now >= ann["once_ts"] and now - ann["once_ts"] <= 120 and last < ann["once_ts"]:
                    should_fire = True

            if not should_fire:
                continue

            # Fire the announcement -- use stored channel_idx for reliable delivery
            ch_idx = ann.get("channel_idx")  # may be None for rows created before 0.9.6
            if ch_idx is None:
                # Fallback: resolve by name at fire time
                ch_row = await dispatcher.get_channel_by_name(ann["channel"])
                if ch_row:
                    ch_idx = ch_row["channel_idx"]
            logger.info(
                f"Firing announcement '{ann['slug']}' -> #{ann['channel']} (slot {ch_idx})"
            )
            await dispatcher.reply_queue.put({
                "target_id":   None,
                "channel":     ann["channel"],
                "channel_idx": ch_idx,
                "text":        ann["text"],
                "part": 1, "total": 1,
            })
            await db.execute(
                "UPDATE announcements SET last_sent_ts=? WHERE id=?",
                (now, ann["id"]),
            )
            # Deactivate one-shot announcements after firing
            if not ann["recurring"]:
                await db.execute(
                    "UPDATE announcements SET active=0 WHERE id=?",
                    (ann["id"],)
                )
                logger.info(f"One-shot announcement '{ann['slug']}' deactivated after firing.")
            await db.commit()

    # Start loop on first inbound message
    _loop_started = {"done": False}

    async def _startup_listener(msg):
        if not _loop_started["done"]:
            _loop_started["done"] = True
            asyncio.create_task(_announce_loop())

    dispatcher.register_listener(_startup_listener)
