"""
Plugin: Network Statistics
Commands:
  !stats              — Summary across all sections
  !stats messages     — Message volume (24h / 7d) and top senders
  !stats users        — Active users (24h / 7d)
  !stats channels     — Most active channels (7d)
  !stats commands     — Top commands by usage this session
  !stats alerts       — NWS alerts stored (7d)
  !stats uptime       — Bot process uptime and start time
  !stats wx           — ZIP lookup cache hit rate since last restart

All sections are DM only, admin only.

Config: none (no config/plugins/stats.yaml needed)
"""

__version__ = "0.1.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import time
from datetime import datetime, timezone
from core.database import PRIV_ADMIN

# Number of top entries to show per ranked section.
TOP_N = 5


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def setup(dispatcher, config, db):

    def _now() -> int:
        return int(time.time())

    # ── Section builders ──────────────────────────────────────────────────────

    async def _section_messages() -> str:
        now   = _now()
        h24   = now - 86400
        d7    = now - 604800

        cnt24 = (await db.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE ts >= ?", (h24,)
        ))["n"]
        cnt7  = (await db.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE ts >= ?", (d7,)
        ))["n"]
        total = (await db.fetchone("SELECT COUNT(*) AS n FROM messages"))["n"]

        top = await db.fetchall(
            """SELECT sender_name, sender_id, COUNT(*) AS n
               FROM messages WHERE ts >= ?
               GROUP BY sender_id ORDER BY n DESC LIMIT ?""",
            (d7, TOP_N),
        )

        lines = [
            f"Messages: {cnt24} (24h)  {cnt7} (7d)  {total} (all time)",
            f"Top senders (7d):",
        ]
        for r in top:
            name = r["sender_name"] or r["sender_id"]
            lines.append(f"  {name}: {r['n']}")
        return "\n".join(lines)

    async def _section_users() -> str:
        now = _now()
        h24 = now - 86400
        d7  = now - 604800

        active24 = (await db.fetchone(
            "SELECT COUNT(DISTINCT sender_id) AS n FROM messages WHERE ts >= ?", (h24,)
        ))["n"]
        active7  = (await db.fetchone(
            "SELECT COUNT(DISTINCT sender_id) AS n FROM messages WHERE ts >= ?", (d7,)
        ))["n"]
        total    = (await db.fetchone("SELECT COUNT(*) AS n FROM users"))["n"]

        return (
            f"Users: {active24} active (24h)  {active7} active (7d)  {total} known"
        )

    async def _section_channels() -> str:
        d7 = _now() - 604800
        rows = await db.fetchall(
            """SELECT channel, COUNT(*) AS n
               FROM messages
               WHERE ts >= ? AND channel IS NOT NULL
               GROUP BY channel ORDER BY n DESC LIMIT ?""",
            (d7, TOP_N),
        )
        if not rows:
            return "Channels: no channel messages in last 7d"
        lines = ["Most active channels (7d):"]
        for r in rows:
            lines.append(f"  {r['channel']}: {r['n']} msgs")
        # Also count DMs
        dm_cnt = (await db.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE ts >= ? AND channel IS NULL",
            (d7,)
        ))["n"]
        lines.append(f"  (DMs): {dm_cnt} msgs")
        return "\n".join(lines)

    async def _section_commands() -> str:
        usage = dispatcher.cmd_usage
        if not usage:
            return "Commands: none dispatched this session"
        cc = dispatcher.command_char
        top = sorted(usage.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
        total = sum(usage.values())
        lines = [f"Commands this session: {total} total"]
        for cmd, count in top:
            display = cc + cmd.lstrip("!")
            lines.append(f"  {display}: {count}")
        return "\n".join(lines)

    async def _section_alerts() -> str:
        d7  = _now() - 604800
        try:
            cnt7  = (await db.fetchone(
                "SELECT COUNT(*) AS n FROM wx_alerts WHERE ts >= ?", (d7,)
            ))["n"]
            active = (await db.fetchone(
                """SELECT COUNT(*) AS n FROM wx_alerts
                   WHERE expires IS NULL OR expires > ?""", (_now(),)
            ))["n"]
            total = (await db.fetchone("SELECT COUNT(*) AS n FROM wx_alerts"))["n"]
            return (
                f"Alerts: {cnt7} stored (7d)  {active} active now  {total} all time"
            )
        except Exception:
            return "Alerts: weather plugin not loaded or no alert data"

    async def _section_uptime() -> str:
        now     = _now()
        elapsed = time.time() - dispatcher.started_at
        started = _fmt_ts(int(dispatcher.started_at))
        return f"Uptime: {_fmt_uptime(elapsed)} (started {started})"

    async def _section_wx() -> str:
        """ZIP forecast cache stats from wx_forecast table."""
        try:
            total_fetches = (await db.fetchone(
                "SELECT COUNT(*) AS n FROM wx_forecast"
            ))["n"]
            zip_fetches = (await db.fetchone(
                "SELECT COUNT(*) AS n FROM wx_forecast WHERE zone LIKE 'zip:%'"
            ))["n"]
            home_fetches = total_fetches - zip_fetches
            # Approximate cache hits: commands dispatched vs live fetches made.
            # We count !wx and !alerts dispatches from the session counter.
            usage    = dispatcher.cmd_usage
            wx_calls = usage.get("!wx", 0) + usage.get("!alerts", 0)
            if wx_calls > 0 and total_fetches > 0:
                # fetches is an upper bound on cache misses
                misses    = min(zip_fetches, wx_calls)
                hits      = max(0, wx_calls - misses)
                hit_pct   = int(100 * hits / wx_calls)
                cache_note = f"{hit_pct}% est. cache hit rate ({wx_calls} calls, {zip_fetches} live fetches)"
            else:
                cache_note = "no data yet this session"
            return (
                f"ZIP cache: {zip_fetches} live fetches (ZIP)  {home_fetches} (home)\n"
                f"  {cache_note}"
            )
        except Exception:
            return "Weather: plugin not loaded or no forecast data"

    SECTIONS = {
        "messages": _section_messages,
        "users":    _section_users,
        "channels": _section_channels,
        "commands": _section_commands,
        "alerts":   _section_alerts,
        "uptime":   _section_uptime,
        "wx":       _section_wx,
    }

    # ── Command ───────────────────────────────────────────────────────────────

    async def cmd_stats(msg):
        arg = msg.arg_str.strip().lower()

        if arg and arg not in SECTIONS:
            valid = "  ".join(SECTIONS.keys())
            return f"Unknown section '{arg}'. Valid: {valid}"

        if arg:
            return await SECTIONS[arg]()

        # Summary — run all sections and join with blank line separators.
        # Each section is kept brief so the full summary fits in ~3 chunks.
        parts = []
        for fn in SECTIONS.values():
            try:
                parts.append(await fn())
            except Exception as e:
                parts.append(f"[error: {e}]")
        return "\n\n".join(parts)

    dispatcher.register_admin_command(
        "!stats", cmd_stats,
        help_text="Network and bot statistics",
        usage_text=(
            "!stats                — full summary\n"
            "!stats messages       — message volume and top senders\n"
            "!stats users          — active user counts\n"
            "!stats channels       — most active channels\n"
            "!stats commands       — top commands this session\n"
            "!stats alerts         — NWS alert counts\n"
            "!stats uptime         — bot uptime\n"
            "!stats wx             — ZIP cache hit rate"
        ),
        scope="direct", priv_floor=PRIV_ADMIN,
        category="admin", plugin_name="stats",
    )
