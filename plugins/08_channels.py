"""
Channel management plugin.

Enumerates channel slots from the radio at startup and rehash, stores
respond flags and safety state in the _channels DB table, and exposes
!channel list, !channel set, and !channel sync subcommands.

Channel data comes from the radio — no config.yaml entries needed.
The radio is the source of truth for slot names. The bot's only
stored opinion is whether to respond on each slot (respond flag)
and whether a safety disable is active (disabled_at).

Wiring: meshhall.py calls _inject_conn(conn) after the ConnectionManager
is created. Until then, conn is None and sync operations return a notice.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
__version__   = "0.2.0"

import logging

logger = logging.getLogger(__name__)

_conn = None


def _inject_conn(conn):
    """Called by meshhall.py after ConnectionManager is created."""
    global _conn
    _conn = conn


def setup(dispatcher, config, db):

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def do_list(msg, args=""):
        """List all channel slots enumerated from the radio."""
        rows = await db.fetchall(
            "SELECT channel_idx, name, respond, disabled_at, last_seen "
            "FROM _channels ORDER BY channel_idx"
        )
        if not rows:
            cc = dispatcher.command_char
            return (
                "No channels enumerated yet.\n"
                f"Use {cc}channel sync to read from radio."
            )
        cc    = dispatcher.command_char
        lines = [f"Channels ({len(rows)})  |  {cc}channel set <idx> on|off"]
        for r in rows:
            idx         = r["channel_idx"]
            name        = r["name"]
            disabled_at = r["disabled_at"]
            respond     = r["respond"]
            if disabled_at:
                status = f"DISABLED (since {_fmt_ts(disabled_at)})"
            elif respond:
                status = "respond=on"
            else:
                status = "respond=off"
            lines.append(f"[{idx}] {name}: {status}")
        return "\n".join(lines)

    async def do_set(msg, args=""):
        """Set respond flag on a channel slot: !channel set <idx> on|off"""
        cc   = dispatcher.command_char
        args = args.strip().split()
        if len(args) < 2:
            return f"Usage: {cc}channel set <idx> on|off"

        try:
            idx = int(args[0])
        except ValueError:
            return f"Invalid slot index: {args[0]!r} — must be an integer 0-7."

        action = args[1].lower()
        if action not in ("on", "off"):
            return f"Invalid action: {args[1]!r} — use 'on' or 'off'."

        row = await db.fetchone(
            "SELECT name, respond, disabled_at FROM _channels WHERE channel_idx=?",
            (idx,),
        )
        if row is None:
            return (
                f"Slot {idx} not found in channel table.\n"
                f"Run {cc}channel sync to enumerate from radio."
            )

        respond     = 1 if action == "on" else 0
        disabled_at = None

        dispatcher.log_admin_attempt(
            f"{cc}channel set {idx} {action}", msg, granted=True,
            reason=f"slot {idx} ({row['name']!r})",
        )
        await db.execute(
            "UPDATE _channels SET respond=?, disabled_at=? WHERE channel_idx=?",
            (respond, disabled_at, idx),
        )
        await db.commit()

        if _conn is not None:
            await _conn._reload_channel_cache()

        verb = "will now respond" if respond else "will no longer respond"
        return f"Slot {idx} ({row['name']!r}): {verb} in channel."

    async def do_sync(msg, args=""):
        """Re-enumerate all slots from the radio."""
        if _conn is None:
            return "Connection manager not available — cannot sync."
        cc = dispatcher.command_char
        dispatcher.log_admin_attempt(f"{cc}channel sync", msg, granted=True)
        return await _conn.enumerate_channels()

    _SUBCOMMANDS = {
        "list": do_list,
        "set":  do_set,
        "sync": do_sync,
    }
    _ADMIN_SUBS = {"set", "sync"}

    # ── Subcommand dispatcher ─────────────────────────────────────────────────

    from core.database import PRIV_ADMIN, PRIV_DEFAULT

    async def cmd_channel(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc = dispatcher.command_char
            return (
                f"Channel commands:\n"
                f"  {cc}channel list            — list all channel slots\n"
                f"  {cc}channel set <idx> on|off — enable/disable responding\n"
                f"  {cc}channel sync            — re-enumerate slots from radio"
            )

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}channel for the list."

        if sub in _ADMIN_SUBS:
            privilege = await db.get_privilege(msg.sender_id)
            if privilege < PRIV_ADMIN:
                return f"Access denied. !channel {sub} requires admin privilege."

        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_command(
        "!channel", cmd_channel,
        help_text="Channel slot management — list, configure, and sync channels",
        usage_text="!channel <list|set|sync> [args]",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="channels", plugin_name="channels",
    )

    # ── Rehash callback ───────────────────────────────────────────────────────

    async def on_rehash():
        if _conn is None:
            return None
        logger.info("Rehash: re-enumerating channel slots from radio.")
        return await _conn.enumerate_channels()

    dispatcher.register_rehash_callback(on_rehash)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(epoch: int) -> str:
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(epoch)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(epoch)
