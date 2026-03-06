"""
Plugin: Nets — Named check-in nets with recurring sessions
Commands (user):
  !checkin <net>      — Check in to a net (DM or bound channel)
  !regrets <net>      — Register planned absence for next session
  !roll [net] [date]  — Roll call for current/recent session (date: YYYY-MM-DD)
  !nets               — List all active nets
  !netinfo <net>      — Net details, next session, schedule

Commands (net control / admin):
  !mknet <slug> <name>           — Create a net
  !rmnet <slug>                  — Deactivate a net
  !addmember <net> <user>        — Add expected member to net
  !delmember <net> <user>        — Remove member from net
  !promote <net> <user>          — Promote guest check-in to full member
  !ncgrant <net> <user>          — Grant net control to a user
  !ncrevoke <net> <user>         — Revoke net control from a user

Recurrence:
  Nets use cron expressions internally (via croniter).
  !mknet accepts human-readable schedule strings:
    weekly <day> <HH:MM>         e.g. "weekly tuesday 19:00"
    monthly <Nth> <day> <HH:MM>  e.g. "monthly 3rd tuesday 19:00"
    daily <HH:MM>                e.g. "daily 08:00"
  Timezone is per-net (IANA string). Defaults to bot config bot.timezone.

Channel binding:
  A net bound to a channel auto-resolves !checkin in that channel.
  !roll in a channel shows net(s) bound to that channel.
  Set channel at !mknet time or via !netset channel <net> <channel>.

Config: config/plugins/nets.yaml
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
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.database import PRIV_DEFAULT, PRIV_ADMIN

logger = logging.getLogger(__name__)

# Minimum privilege floor for net creation (operator-configurable, min 2).
NET_CREATE_PRIV_DEFAULT = 15
NET_CREATE_PRIV_FLOOR   = 2

SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS nets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    description  TEXT,
    channel      TEXT,
    allow_guests INTEGER NOT NULL DEFAULT 0,
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    duration_min INTEGER NOT NULL DEFAULT 60,
    cron_expr    TEXT,
    cron_human   TEXT,
    created_by   TEXT NOT NULL,
    created_ts   INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_nets_slug    ON nets(slug);
CREATE INDEX IF NOT EXISTS idx_nets_channel ON nets(channel);

CREATE TABLE IF NOT EXISTS net_members (
    net_id       INTEGER NOT NULL,
    pubkey_prefix TEXT NOT NULL,
    added_by     TEXT NOT NULL,
    added_ts     INTEGER NOT NULL,
    PRIMARY KEY (net_id, pubkey_prefix),
    FOREIGN KEY (net_id) REFERENCES nets(id)
);

CREATE TABLE IF NOT EXISTS net_control (
    net_id        INTEGER NOT NULL,
    pubkey_prefix TEXT NOT NULL,
    granted_by    TEXT NOT NULL,
    granted_ts    INTEGER NOT NULL,
    PRIMARY KEY (net_id, pubkey_prefix),
    FOREIGN KEY (net_id) REFERENCES nets(id)
);

CREATE TABLE IF NOT EXISTS net_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    net_id     INTEGER NOT NULL,
    opened_ts  INTEGER NOT NULL,
    closed_ts  INTEGER,
    announced  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (net_id) REFERENCES nets(id)
);
CREATE INDEX IF NOT EXISTS idx_net_sessions_net ON net_sessions(net_id);

CREATE TABLE IF NOT EXISTS net_checkins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    pubkey_prefix TEXT NOT NULL,
    display_name  TEXT,
    ts            INTEGER NOT NULL,
    is_guest      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'in',
    FOREIGN KEY (session_id) REFERENCES net_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_net_checkins_session ON net_checkins(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_net_checkins_unique
    ON net_checkins(session_id, pubkey_prefix);
"""

# ── Recurrence parser ─────────────────────────────────────────────────────────

_DAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
             "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def _parse_time(t: str):
    """Parse HH:MM string → (hour, minute) or raise ValueError."""
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time {t!r} — use HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time {t!r}")
    return h, m


def parse_schedule(text: str) -> tuple:
    """
    Parse a human-readable schedule string into (cron_expr, cron_human).
    Accepted formats:
      weekly <day> <HH:MM>
      monthly <Nth> <day> <HH:MM>
      daily <HH:MM>
    Returns (cron_expr, cron_human) or raises ValueError with a helpful message.
    """
    tokens = text.strip().lower().split()
    if not tokens:
        raise ValueError("Schedule cannot be empty.")

    kind = tokens[0]

    if kind == "daily":
        if len(tokens) != 2:
            raise ValueError("Usage: daily HH:MM")
        h, m = _parse_time(tokens[1])
        return f"{m} {h} * * *", f"Daily at {tokens[1]}"

    if kind == "weekly":
        if len(tokens) != 3:
            raise ValueError("Usage: weekly <day> HH:MM")
        day_str, time_str = tokens[1], tokens[2]
        if day_str not in _DAYS:
            raise ValueError(f"Unknown day {day_str!r}. Use monday, tuesday, etc.")
        h, m = _parse_time(time_str)
        dow = _DAYS[day_str]
        return f"{m} {h} * * {dow}", f"Weekly on {day_str.capitalize()} at {time_str}"

    if kind == "monthly":
        if len(tokens) != 4:
            raise ValueError("Usage: monthly <Nth> <day> HH:MM  e.g. monthly 3rd tuesday 19:00")
        ord_str, day_str, time_str = tokens[1], tokens[2], tokens[3]
        if ord_str not in _ORDINALS:
            raise ValueError(f"Unknown ordinal {ord_str!r}. Use 1st, 2nd, 3rd, 4th, 5th.")
        if day_str not in _DAYS:
            raise ValueError(f"Unknown day {day_str!r}. Use monday, tuesday, etc.")
        n   = _ORDINALS[ord_str]
        dow = _DAYS[day_str]
        h, m = _parse_time(time_str)
        # croniter supports DOW#N syntax for Nth weekday of month
        return (
            f"{m} {h} * * {dow}#{n}",
            f"Monthly on the {ord_str} {day_str.capitalize()} at {time_str}",
        )

    raise ValueError(
        f"Unknown schedule type {kind!r}. Use: daily, weekly, or monthly."
    )


def _tz(tz_str: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        logger.warning(f"Unknown timezone {tz_str!r}, falling back to UTC")
        return ZoneInfo("UTC")


def _next_occurrence(cron_expr: str, tz_str: str) -> Optional[datetime]:
    """Return next scheduled datetime (timezone-aware) or None if croniter unavailable."""
    try:
        from croniter import croniter
    except ImportError:
        return None
    try:
        tz   = _tz(tz_str)
        now  = datetime.now(tz)
        cron = croniter(cron_expr, now)
        return cron.get_next(datetime).replace(tzinfo=tz)
    except Exception as e:
        logger.warning(f"croniter error for {cron_expr!r}: {e}")
        return None


def _fmt_next(cron_expr: str, tz_str: str) -> str:
    dt = _next_occurrence(cron_expr, tz_str)
    if dt is None:
        return "unknown (croniter not installed)"
    return dt.strftime("%a %Y-%m-%d %H:%Mz").rstrip("z") + " " + tz_str


def _now() -> int:
    return int(time.time())


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


# ── Plugin setup ──────────────────────────────────────────────────────────────

def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    def _cfg():
        return config.plugin("nets")

    def _default_tz() -> str:
        return config.get("bot.timezone", "UTC")

    def _create_priv() -> int:
        raw   = _cfg().get("create_privilege", NET_CREATE_PRIV_DEFAULT)
        floor = NET_CREATE_PRIV_FLOOR
        try:
            return max(floor, min(15, int(raw)))
        except (TypeError, ValueError):
            return NET_CREATE_PRIV_DEFAULT

    # ── Validation helpers ────────────────────────────────────────────────────

    def _valid_slug(slug: str) -> bool:
        return bool(SLUG_RE.match(slug)) and len(slug) <= 32

    async def _get_net(slug: str):
        return await db.fetchone(
            "SELECT * FROM nets WHERE slug=? AND active=1", (slug,)
        )

    async def _is_net_control(net_id: int, pubkey_prefix: str) -> bool:
        row = await db.fetchone(
            "SELECT 1 FROM net_control WHERE net_id=? AND pubkey_prefix=?",
            (net_id, pubkey_prefix),
        )
        return row is not None

    async def _is_member(net_id: int, pubkey_prefix: str) -> bool:
        row = await db.fetchone(
            "SELECT 1 FROM net_members WHERE net_id=? AND pubkey_prefix=?",
            (net_id, pubkey_prefix),
        )
        return row is not None

    async def _require_nc_or_admin(net_id: int, msg, privilege: int) -> bool:
        """Return True if sender is net control or admin."""
        if privilege >= PRIV_ADMIN:
            return True
        return await _is_net_control(net_id, msg.sender_id)

    # ── Session helpers ───────────────────────────────────────────────────────

    async def _current_session(net_id: int):
        """Return the currently open session for a net, or None."""
        return await db.fetchone(
            """SELECT * FROM net_sessions
               WHERE net_id=? AND closed_ts IS NULL
               ORDER BY opened_ts DESC LIMIT 1""",
            (net_id,),
        )

    async def _latest_session(net_id: int):
        """Return most recent session (open or closed)."""
        return await db.fetchone(
            """SELECT * FROM net_sessions
               WHERE net_id=?
               ORDER BY opened_ts DESC LIMIT 1""",
            (net_id,),
        )

    async def _session_for_date(net_id: int, date_str: str):
        """Return session whose open date matches YYYY-MM-DD (UTC)."""
        try:
            dt    = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start = int(dt.timestamp())
            end   = start + 86400
        except ValueError:
            return None
        return await db.fetchone(
            """SELECT * FROM net_sessions
               WHERE net_id=? AND opened_ts >= ? AND opened_ts < ?
               ORDER BY opened_ts DESC LIMIT 1""",
            (net_id, start, end),
        )

    async def _open_session(net: dict) -> int:
        """Open a new session for the net. Returns session id."""
        cur = await db.execute(
            "INSERT INTO net_sessions (net_id, opened_ts) VALUES (?,?)",
            (net["id"], _now()),
        )
        await db.commit()
        return cur.lastrowid

    async def _close_session(session_id: int):
        await db.execute(
            "UPDATE net_sessions SET closed_ts=? WHERE id=?",
            (_now(), session_id),
        )
        await db.commit()

    # ── Roll call builder ─────────────────────────────────────────────────────

    async def _build_roll(net: dict, session: dict) -> str:
        net_name  = net["name"]
        opened    = _fmt_ts(session["opened_ts"])
        closed    = f" closed {_fmt_ts(session['closed_ts'])}" if session["closed_ts"] else " (open)"

        # All expected members
        members = await db.fetchall(
            """SELECT nm.pubkey_prefix, u.display_name
               FROM net_members nm
               LEFT JOIN users u ON u.pubkey_prefix = nm.pubkey_prefix
               WHERE nm.net_id=?""",
            (net["id"],),
        )
        # All checkins for this session
        checkins = await db.fetchall(
            "SELECT * FROM net_checkins WHERE session_id=?",
            (session["id"],),
        )
        checked_in = {r["pubkey_prefix"]: r for r in checkins}

        lines = [f"Roll — {net_name} {opened}{closed}"]

        # Members first
        for m in members:
            pk   = m["pubkey_prefix"]
            name = m["display_name"] or pk
            ci   = checked_in.get(pk)
            if ci:
                if ci["status"] == "regrets":
                    lines.append(f"  ✗ {name} (regrets)")
                else:
                    lines.append(f"  ✓ {name}")
            else:
                lines.append(f"  — {name}")

        # Guests — checked in but not in net_members
        member_ids = {m["pubkey_prefix"] for m in members}
        guests = [r for r in checkins
                  if r["pubkey_prefix"] not in member_ids and r["is_guest"]]
        for g in guests:
            name = g["display_name"] or g["pubkey_prefix"]
            if g["status"] == "regrets":
                lines.append(f"  ✗ {name} (guest, regrets)")
            else:
                lines.append(f"  ✓ {name} (guest)")

        checked_count  = sum(1 for r in checkins if r["status"] == "in")
        regrets_count  = sum(1 for r in checkins if r["status"] == "regrets")
        lines.append(f"Total: {checked_count} in, {regrets_count} regrets")
        return "\n".join(lines)

    # ── Announcement helper ───────────────────────────────────────────────────

    async def _announce(net: dict, text: str):
        channel = net["channel"]
        if not channel:
            return
        await dispatcher.reply_queue.put({
            "target_id":   None,
            "channel":     channel,
            "channel_idx": None,
            "text":        text,
            "part": 1, "total": 1,
        })

    # ── Background session scheduler ──────────────────────────────────────────

    async def _session_loop():
        """
        Poll every 60 seconds. For each active net with a cron schedule:
          - Open a session if one is due and none is open.
          - Close a session if it has exceeded its duration.
          - Announce open/close in the bound channel.
        """
        await asyncio.sleep(10)
        while True:
            try:
                await _tick_sessions()
            except Exception as e:
                logger.error(f"Nets session loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _tick_sessions():
        now  = _now()
        nets = await db.fetchall(
            "SELECT * FROM nets WHERE active=1 AND cron_expr IS NOT NULL"
        )
        for net in nets:
            try:
                from croniter import croniter
            except ImportError:
                break  # croniter not installed — skip scheduling

            net_id       = net["id"]
            cron_expr    = net["cron_expr"]
            duration_sec = net["duration_min"] * 60
            tz           = _tz(net["timezone"])

            # Check for open session first
            open_sess = await _current_session(net_id)
            if open_sess:
                # Close if past duration
                age = now - open_sess["opened_ts"]
                if age >= duration_sec:
                    await _close_session(open_sess["id"])
                    checkin_count = (await db.fetchone(
                        "SELECT COUNT(*) AS n FROM net_checkins WHERE session_id=? AND status='in'",
                        (open_sess["id"],),
                    ))["n"]
                    await _announce(
                        net,
                        f"📡 {net['name']} net is now closed. "
                        f"{checkin_count} checked in.",
                    )
                continue

            # No open session — check if one is due
            try:
                now_dt   = datetime.now(tz)
                cron_it  = croniter(cron_expr, now_dt)
                prev_dt  = cron_it.get_prev(datetime).replace(tzinfo=tz)
                prev_ts  = int(prev_dt.timestamp())
                # Due if the previous occurrence is within the last 2 minutes
                # and we haven't opened a session for it yet.
                if now - prev_ts <= 120:
                    # Check we haven't already opened a session near this time
                    existing = await db.fetchone(
                        """SELECT id FROM net_sessions
                           WHERE net_id=? AND opened_ts >= ?""",
                        (net_id, prev_ts - 60),
                    )
                    if not existing:
                        sess_id = await _open_session(net)
                        cc = dispatcher.command_char
                        await _announce(
                            net,
                            f"📡 {net['name']} net is now open. "
                            f"Check in with {cc}checkin {net['slug']}",
                        )
            except Exception as e:
                logger.warning(f"Session schedule check failed for {net['slug']}: {e}")

    started = {"done": False}

    async def startup_listener(msg):
        if not started["done"]:
            asyncio.create_task(_session_loop())
            started["done"] = True

    dispatcher.register_listener(startup_listener)

    # ── !checkin ──────────────────────────────────────────────────────────────

    async def do_checkin(msg, args=""):
        arg = args.strip().lower()

        # Resolve net slug: explicit arg > channel binding > single-member auto
        slug = None
        if arg:
            slug = arg
        elif msg.channel:
            # Channel binding — find net bound to this channel
            bound = await db.fetchone(
                "SELECT slug FROM nets WHERE channel=? AND active=1", (msg.channel,)
            )
            if bound:
                slug = bound["slug"]
        if not slug:
            # DM with no arg — auto-resolve if user is in exactly one active net
            member_nets = await db.fetchall(
                """SELECT n.slug FROM nets n
                   JOIN net_members nm ON nm.net_id = n.id
                   WHERE nm.pubkey_prefix=? AND n.active=1""",
                (msg.sender_id,),
            )
            if len(member_nets) == 1:
                slug = member_nets[0]["slug"]
            elif len(member_nets) > 1:
                slugs = ", ".join(r["slug"] for r in member_nets)
                return f"You're on multiple nets. Specify one: !checkin <net>  ({slugs})"
            else:
                return "No net specified. Use !checkin <net-slug>"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        # Must be a member or guests allowed
        is_member = await _is_member(net["id"], msg.sender_id)
        if not is_member and not net["allow_guests"]:
            return f"You're not on the member list for {net['name']}. Ask net control to add you."

        # Find current session
        session = await _current_session(net["id"])
        if not session:
            return f"{net['name']} net is not currently in session."

        # Upsert checkin (idempotent — re-checking in updates timestamp)
        existing = await db.fetchone(
            "SELECT id, status FROM net_checkins WHERE session_id=? AND pubkey_prefix=?",
            (session["id"], msg.sender_id),
        )
        name = msg.sender_name or msg.sender_id
        if existing:
            if existing["status"] == "in":
                return f"Already checked in to {net['name']}. ✓"
            # Was regrets — update to in
            await db.execute(
                "UPDATE net_checkins SET status='in', ts=?, display_name=? WHERE id=?",
                (_now(), name, existing["id"]),
            )
        else:
            await db.execute(
                """INSERT INTO net_checkins
                   (session_id, pubkey_prefix, display_name, ts, is_guest, status)
                   VALUES (?,?,?,?,?,?)""",
                (session["id"], msg.sender_id, name, _now(),
                 0 if is_member else 1, "in"),
            )
        await db.commit()
        guest_note = " (guest)" if not is_member else ""
        return f"Checked in to {net['name']}{guest_note}. ✓"

    # ── !regrets ──────────────────────────────────────────────────────────────

    async def do_regrets(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !regrets <net>"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        is_member = await _is_member(net["id"], msg.sender_id)
        if not is_member and not net["allow_guests"]:
            return f"You're not on the member list for {net['name']}."

        session = await _current_session(net["id"])
        if not session:
            return (
                f"{net['name']} is not currently in session. "
                f"Regrets noted — let net control know directly."
            )

        name = msg.sender_name or msg.sender_id
        existing = await db.fetchone(
            "SELECT id, status FROM net_checkins WHERE session_id=? AND pubkey_prefix=?",
            (session["id"], msg.sender_id),
        )
        if existing:
            await db.execute(
                "UPDATE net_checkins SET status='regrets', ts=?, display_name=? WHERE id=?",
                (_now(), name, existing["id"]),
            )
        else:
            await db.execute(
                """INSERT INTO net_checkins
                   (session_id, pubkey_prefix, display_name, ts, is_guest, status)
                   VALUES (?,?,?,?,?,?)""",
                (session["id"], msg.sender_id, name, _now(),
                 0 if is_member else 1, "regrets"),
            )
        await db.commit()
        return f"Regrets logged for {net['name']}. You'll be marked absent on the roll."

    # ── !roll ─────────────────────────────────────────────────────────────────

    async def do_roll(msg, args=""):
        import re as _re
        parts    = args.strip().split()
        date_str = None
        slug     = None

        for p in parts:
            if _re.match(r'^\d{4}-\d{2}-\d{2}$', p):
                date_str = p
            elif not slug:
                slug = p.lower()

        if not slug and msg.channel:
            bound = await db.fetchone(
                "SELECT slug FROM nets WHERE channel=? AND active=1", (msg.channel,)
            )
            if bound:
                slug = bound["slug"]

        if not slug:
            return "Usage: !roll <net> [YYYY-MM-DD]"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        if date_str:
            session = await _session_for_date(net["id"], date_str)
            if not session:
                return f"No session found for {net['name']} on {date_str}."
        else:
            session = await _current_session(net["id"])
            if not session:
                session = await _latest_session(net["id"])
            if not session:
                return f"No sessions on record for {net['name']}."

        return await _build_roll(net, session)

    # ── !nets ─────────────────────────────────────────────────────────────────

    async def do_list(msg, args=""):
        rows = await db.fetchall(
            "SELECT slug, name, channel, allow_guests FROM nets WHERE active=1 ORDER BY name"
        )
        if not rows:
            return "No active nets."
        lines = ["Active nets:"]
        for r in rows:
            ch    = f" [{r['channel']}]" if r["channel"] else ""
            guest = " (guests OK)" if r["allow_guests"] else ""
            lines.append(f"  {r['slug']}: {r['name']}{ch}{guest}")
        return "\n".join(lines)

    # ── !netinfo ──────────────────────────────────────────────────────────────

    async def do_info(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !netinfo <net>"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        lines = [f"{net['name']} ({net['slug']})"]
        if net["description"]:
            lines.append(net["description"])

        if net["cron_expr"]:
            lines.append(f"Next: {_fmt_next(net['cron_expr'], net['timezone'])}")
            lines.append(f"Schedule: {net['cron_human']}")
        else:
            lines.append("Schedule: manual / no recurrence")

        lines.append(f"Duration: {net['duration_min']} min")
        lines.append(f"Timezone: {net['timezone']}")

        ch = net["channel"] or "none"
        lines.append(f"Channel: {ch}")
        lines.append(f"Guests: {'allowed' if net['allow_guests'] else 'members only'}")

        member_count = (await db.fetchone(
            "SELECT COUNT(*) AS n FROM net_members WHERE net_id=?", (net["id"],)
        ))["n"]
        lines.append(f"Members: {member_count}")

        session = await _current_session(net["id"])
        if session:
            lines.append(f"Status: OPEN since {_fmt_ts(session['opened_ts'])}")
        else:
            lines.append("Status: closed")

        return "\n".join(lines)

    # ── !mknet ────────────────────────────────────────────────────────────────

    async def do_create(msg, args=""):
        raw = args.strip()
        if not raw:
            return (
                "Usage: !net create <slug> <n> [schedule \"...\" ] [timezone TZ] "
                "[duration MIN] [channel CH] [guests yes|no] [description \"...\" ]"
            )

        tokens = raw.split(None, 1)
        if len(tokens) < 2:
            return "Usage: !net create <slug> <n> ..."

        slug = tokens[0].lower()
        if not _valid_slug(slug):
            return (
                "Invalid slug. Use lowercase letters, numbers, hyphens only "
                "(e.g. ares-district-5). Max 32 chars."
            )

        existing = await db.fetchone("SELECT id FROM nets WHERE slug=?", (slug,))
        if existing:
            return f"A net with slug '{slug}' already exists."

        rest = tokens[1]
        opts = _parse_mknet_opts(rest)
        name = opts.get("_name", "").strip()
        if not name:
            return "Net name is required."

        cron_expr  = None
        cron_human = None
        sched_raw  = opts.get("schedule", "")
        if sched_raw:
            try:
                cron_expr, cron_human = parse_schedule(sched_raw)
            except ValueError as e:
                return f"Schedule error: {e}"

        tz_str = opts.get("timezone", _default_tz())
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(tz_str)
        except ZoneInfoNotFoundError:
            return f"Unknown timezone '{tz_str}'. Use IANA names e.g. America/Los_Angeles"

        try:
            duration = int(opts.get("duration", 60))
        except ValueError:
            return "Duration must be an integer number of minutes."

        channel      = opts.get("channel", "") or None
        allow_guests = opts.get("guests", "no").lower() in ("yes", "true", "1")
        description  = opts.get("description", "") or None

        cur = await db.execute(
            """INSERT INTO nets
               (slug, name, description, channel, allow_guests, timezone,
                duration_min, cron_expr, cron_human, created_by, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, name, description, channel, 1 if allow_guests else 0,
             tz_str, duration, cron_expr, cron_human,
             msg.sender_id, _now()),
        )
        await db.commit()

        sched_note = f", {cron_human}" if cron_human else ", no schedule"
        return f"Net '{slug}' ({name}) created{sched_note}. Use !net info {slug} for details."

    def _parse_mknet_opts(text: str) -> dict:
        import re as _re
        keywords = ("schedule", "timezone", "duration", "channel", "guests", "description")
        result   = {}
        pattern  = r'(?i)\b(' + '|'.join(keywords) + r')\s+'
        parts    = _re.split(pattern, text)
        result["_name"] = parts[0].strip().strip('"')
        i = 1
        while i < len(parts) - 1:
            key = parts[i].lower()
            val = parts[i + 1].strip().strip('"')
            result[key] = val
            i += 2
        return result

    # ── !rmnet ────────────────────────────────────────────────────────────────

    async def do_delete(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !net delete <net>"
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or already inactive."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        await db.execute("UPDATE nets SET active=0 WHERE id=?", (net["id"],))
        await db.commit()
        return f"Net '{slug}' deactivated."

    # ── !addmember ────────────────────────────────────────────────────────────

    async def do_add(msg, args=""):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: !net add <net> <callsign or name>"

        slug, query = parts[0].lower(), parts[1]
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        user = await db.find_user(query)
        if not user:
            return f"User '{query}' not found. They must have contacted the bot at least once."

        if await _is_member(net["id"], user["pubkey_prefix"]):
            return f"{user['display_name'] or user['pubkey_prefix']} is already a member of {net['name']}."

        await db.execute(
            "INSERT INTO net_members (net_id, pubkey_prefix, added_by, added_ts) VALUES (?,?,?,?)",
            (net["id"], user["pubkey_prefix"], msg.sender_id, _now()),
        )
        await db.commit()
        name = user["display_name"] or user["pubkey_prefix"]
        return f"{name} added to {net['name']}."

    # ── !delmember ────────────────────────────────────────────────────────────

    async def do_remove(msg, args=""):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: !net remove <net> <callsign or name>"

        slug, query = parts[0].lower(), parts[1]
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        user = await db.find_user(query)
        if not user:
            return f"User '{query}' not found."

        if not await _is_member(net["id"], user["pubkey_prefix"]):
            return f"{user['display_name'] or user['pubkey_prefix']} is not a member of {net['name']}."

        await db.execute(
            "DELETE FROM net_members WHERE net_id=? AND pubkey_prefix=?",
            (net["id"], user["pubkey_prefix"]),
        )
        await db.commit()
        name = user["display_name"] or user["pubkey_prefix"]
        return f"{name} removed from {net['name']}."

    # ── !promote ──────────────────────────────────────────────────────────────

    async def do_promote(msg, args=""):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: !net promote <net> <callsign or name>"

        slug, query = parts[0].lower(), parts[1]
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        user = await db.find_user(query)
        if not user:
            return f"User '{query}' not found."

        if await _is_member(net["id"], user["pubkey_prefix"]):
            return f"{user['display_name'] or user['pubkey_prefix']} is already a full member."

        session = await _latest_session(net["id"])
        if session:
            await db.execute(
                "UPDATE net_checkins SET is_guest=0 WHERE session_id=? AND pubkey_prefix=?",
                (session["id"], user["pubkey_prefix"]),
            )
        await db.execute(
            "INSERT OR IGNORE INTO net_members (net_id, pubkey_prefix, added_by, added_ts) VALUES (?,?,?,?)",
            (net["id"], user["pubkey_prefix"], msg.sender_id, _now()),
        )
        await db.commit()
        name = user["display_name"] or user["pubkey_prefix"]
        return f"{name} promoted to full member of {net['name']}."

    # ── !ncgrant ──────────────────────────────────────────────────────────────

    async def do_grant(msg, args=""):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: !net grant <net> <callsign or name>"

        slug, query = parts[0].lower(), parts[1]
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found."

        privilege = await db.get_privilege(msg.sender_id)
        if privilege < PRIV_ADMIN:
            return "Access denied. Admin required to grant net control."

        user = await db.find_user(query)
        if not user:
            return f"User '{query}' not found."

        await db.execute(
            "INSERT OR IGNORE INTO net_control (net_id, pubkey_prefix, granted_by, granted_ts) VALUES (?,?,?,?)",
            (net["id"], user["pubkey_prefix"], msg.sender_id, _now()),
        )
        await db.commit()
        name = user["display_name"] or user["pubkey_prefix"]
        return f"{name} granted net control for {net['name']}."

    # ── !ncrevoke ─────────────────────────────────────────────────────────────

    async def do_revoke(msg, args=""):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: !net revoke <net> <callsign or name>"

        slug, query = parts[0].lower(), parts[1]
        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found."

        privilege = await db.get_privilege(msg.sender_id)
        if privilege < PRIV_ADMIN:
            return "Access denied. Admin required to revoke net control."

        user = await db.find_user(query)
        if not user:
            return f"User '{query}' not found."

        await db.execute(
            "DELETE FROM net_control WHERE net_id=? AND pubkey_prefix=?",
            (net["id"], user["pubkey_prefix"]),
        )
        await db.commit()
        name = user["display_name"] or user["pubkey_prefix"]
        return f"{name} net control revoked for {net['name']}."



    # ── !net start ────────────────────────────────────────────────────────────

    async def do_start(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !net start <slug>"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        open_sess = await _current_session(net["id"])
        if open_sess:
            return f"{net['name']} is already in session (opened {_fmt_ts(open_sess['opened_ts'])})."

        sess_id = await _open_session(net)
        cc = dispatcher.command_char
        await _announce(
            net,
            f"📡 {net['name']} net is now open. Check in with {cc}checkin {net['slug']}",
        )
        return f"{net['name']} session opened. Members can now {cc}checkin {slug}."

    # ── !net stop ─────────────────────────────────────────────────────────────

    async def do_stop(msg, args=""):
        slug = args.strip().lower()
        if not slug:
            return "Usage: !net stop <slug>"

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        session = await _current_session(net["id"])
        if not session:
            return f"{net['name']} is not currently in session."

        await _close_session(session["id"])

        checkins = await db.fetchall(
            "SELECT status FROM net_checkins WHERE session_id=?", (session["id"],)
        )
        checked_in = sum(1 for r in checkins if r["status"] == "in")
        regrets    = sum(1 for r in checkins if r["status"] == "regrets")

        summary = f"{checked_in} checked in"
        if regrets:
            summary += f", {regrets} regrets"

        await _announce(net, f"📡 {net['name']} net is now closed. {summary}.")
        return f"{net['name']} session closed. {summary}."

    # ── !net schedule ─────────────────────────────────────────────────────────

    async def do_schedule(msg, args=""):
        """
        !net schedule <slug> <schedule> [timezone]
        !net schedule <slug> none        — clear schedule (ad-hoc only)
        """
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return (
                "Usage: !net schedule <slug> <schedule> [timezone]\n"
                "       !net schedule <slug> none\n"
                "Schedule examples: \'weekly tuesday 19:00\', \'monthly 3rd tuesday 19:00\', \'daily 08:00\'"
            )

        slug      = parts[0].lower()
        remainder = parts[1].strip()

        net = await _get_net(slug)
        if not net:
            return f"Net '{slug}' not found or inactive."

        privilege = await db.get_privilege(msg.sender_id)
        if not await _require_nc_or_admin(net["id"], msg, privilege):
            return "Access denied. Net control or admin required."

        # Clear schedule
        if remainder.lower() in ("none", "clear"):
            await db.execute(
                "UPDATE nets SET cron_expr=NULL, cron_human=NULL WHERE id=?",
                (net["id"],),
            )
            await db.commit()
            return f"{net['name']} schedule cleared. Sessions must now be opened manually with !net start."

        # Parse optional trailing timezone — last token if it contains a /
        tokens    = remainder.rsplit(None, 1)
        tz_str    = None
        sched_str = remainder

        if len(tokens) == 2 and '/' in tokens[-1]:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(tokens[-1])
                tz_str    = tokens[-1]
                sched_str = tokens[0]
            except ZoneInfoNotFoundError:
                pass  # treat as part of schedule string

        if tz_str is None:
            tz_str = net["timezone"]  # keep existing timezone

        # Validate timezone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz_str)
        except ZoneInfoNotFoundError:
            return f"Unknown timezone '{tz_str}'. Use IANA names e.g. America/Los_Angeles"

        try:
            cron_expr, cron_human = parse_schedule(sched_str)
        except ValueError as e:
            return f"Schedule error: {e}"

        await db.execute(
            "UPDATE nets SET cron_expr=?, cron_human=?, timezone=? WHERE id=?",
            (cron_expr, cron_human, tz_str, net["id"]),
        )
        await db.commit()
        return (
            f"{net['name']} schedule updated.\n"
            f"Schedule: {cron_human}\n"
            f"Timezone: {tz_str}\n"
            f"Next: {_fmt_next(cron_expr, tz_str)}"
        )

    # ── Subcommand dispatcher ─────────────────────────────────────────────────

    _SUBCOMMANDS = {
        "checkin":   do_checkin,
        "regrets":   do_regrets,
        "roll":      do_roll,
        "list":      do_list,
        "info":      do_info,
        "create":    do_create,
        "delete":    do_delete,
        "start":     do_start,
        "stop":      do_stop,
        "schedule":  do_schedule,
        "add":       do_add,
        "remove":    do_remove,
        "promote":   do_promote,
        "grant":     do_grant,
        "revoke":    do_revoke,
    }

    _ADMIN_SUBS = {"grant", "revoke"}
    _NC_SUBS    = {"create", "delete", "start", "stop", "schedule",
                   "add", "remove", "promote"}

    async def cmd_net(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc = dispatcher.command_char
            lines = [f"Net commands — use {cc}net <subcommand>:"]
            lines += [
                f"  {cc}net list                        — list all active nets",
                f"  {cc}net info <slug>                 — net details and schedule",
                f"  {cc}net checkin [slug]              — check in to a net (shortcut: {cc}checkin)",
                f"  {cc}net regrets <slug>              — register planned absence (shortcut: {cc}regrets)",
                f"  {cc}net roll [slug] [YYYY-MM-DD]    — roll call (shortcut: {cc}roll)",
                f"  {cc}net start <slug>                — open a session manually (net control/admin)",
                f"  {cc}net stop <slug>                 — close a session manually (net control/admin)",
                f"  {cc}net schedule <slug> <schedule>  — set or clear recurrence (net control/admin)",
                f"  {cc}net create <slug> <n> ...       — create a net (net control/admin)",
                f"  {cc}net delete <slug>               — deactivate a net (net control/admin)",
                f"  {cc}net add <net> <user>            — add member (net control/admin)",
                f"  {cc}net remove <net> <user>         — remove member (net control/admin)",
                f"  {cc}net promote <net> <user>        — promote guest to member",
                f"  {cc}net grant <net> <user>          — grant net control (admin)",
                f"  {cc}net revoke <net> <user>         — revoke net control (admin)",
            ]
            return "\n".join(lines)

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}net for the list."

        # Privilege checks — admin-only and net-control-or-admin subs
        if sub in _ADMIN_SUBS:
            privilege = await db.get_privilege(msg.sender_id)
            if privilege < PRIV_ADMIN:
                return f"Access denied. !net {sub} requires admin privilege."

        # Rewrite arg_str to strip the subcommand token
        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_command(
        "!net", cmd_net,
        help_text="Net management — named check-in nets with recurring sessions",
        usage_text="!net <subcommand>  |  !net for full subcommand list",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="nets", plugin_name="nets", allow_channel=True,
    )

    # ── Standalone shortcut commands ─────────────────────────────────────────
    # These inject the subcommand into arg_str so users can type !checkin <net>
    # instead of !net checkin <net>. Registered as real commands so they
    # work in channels and inherit the correct scope/privilege.

    async def cmd_checkin_shortcut(msg):
        return await cmd_net(msg, ("checkin " + msg.arg_str).strip())

    async def cmd_regrets_shortcut(msg):
        return await cmd_net(msg, ("regrets " + msg.arg_str).strip())

    async def cmd_roll_shortcut(msg):
        return await cmd_net(msg, ("roll " + msg.arg_str).strip())

    dispatcher.register_command(
        "!checkin", cmd_checkin_shortcut,
        help_text="Check in to a net (shortcut for !net checkin)",
        is_shortcut=True,
        usage_text="!checkin [net]  — use !net checkin for full usage",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="nets", plugin_name="nets", allow_channel=True,
    )
    dispatcher.register_command(
        "!regrets", cmd_regrets_shortcut,
        help_text="Register planned absence for a net (shortcut for !net regrets)",
        is_shortcut=True,
        usage_text="!regrets <net>  — use !net regrets for full usage",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="nets", plugin_name="nets",
    )
    dispatcher.register_command(
        "!roll", cmd_roll_shortcut,
        help_text="Net roll call (shortcut for !net roll)",
        is_shortcut=True,
        usage_text="!roll [net] [YYYY-MM-DD]  — use !net roll for full usage",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="nets", plugin_name="nets", allow_channel=True,
    )
