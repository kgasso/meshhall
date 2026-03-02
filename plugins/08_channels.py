"""
Channel management plugin.

Enumerates channel slots from the radio at startup and rehash, stores
respond flags and safety state in the _channels DB table, and exposes
!channels (list) and !channel (admin control) commands.

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
__version__   = "0.1.1"

import logging

logger = logging.getLogger(__name__)

# Module-level conn reference — injected by meshhall.py after ConnectionManager
# is created. Cannot be passed to setup() because plugins load before conn exists.
_conn = None


def _inject_conn(conn):
    """Called by meshhall.py after ConnectionManager is created."""
    global _conn
    _conn = conn


def setup(dispatcher, config, db):

    # ── !channels — list all known slots ─────────────────────────────────────

    async def cmd_channels(msg):
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

        cc = dispatcher.command_char
        lines = [f"Channels ({len(rows)})  |  {cc}channel <idx> on|off|sync"]
        for r in rows:
            idx         = r["channel_idx"]
            name        = r["name"]
            respond     = r["respond"]
            disabled_at = r["disabled_at"]

            if disabled_at:
                status = f"DISABLED (since {_fmt_ts(disabled_at)})"
            elif respond:
                status = "respond=on"
            else:
                status = "respond=off"

            lines.append(f"[{idx}] {name}: {status}")

        return "\n".join(lines)

    dispatcher.register_command(
        "!channels", cmd_channels,
        help_text="List channel slots enumerated from the radio",
        usage_text="!channels",
        scope="direct",
        plugin_name="channels",
        category="channels",
    )

    # ── !channel — admin control ──────────────────────────────────────────────

    async def cmd_channel(msg):
        """
        Admin command: enable/disable responding on a channel slot, or sync
        from the radio.

          !channel sync        — re-enumerate all slots from the radio
          !channel <idx> on    — enable responding on slot <idx>
          !channel <idx> off   — disable responding on slot <idx>
        """
        cc   = dispatcher.command_char
        args = msg.arg_str.strip().split()

        if not args:
            return (
                f"Usage: {cc}channel <idx> on|off  or  {cc}channel sync\n"
                f"See {cc}channels for current slot list."
            )

        # ── sync subcommand ───────────────────────────────────────────────────
        if args[0].lower() == "sync":
            if _conn is None:
                return "Connection manager not available — cannot sync."
            dispatcher.log_admin_attempt(f"{cc}channel sync", msg, granted=True)
            result = await _conn.enumerate_channels()
            return result

        # ── on/off subcommand ─────────────────────────────────────────────────
        if len(args) < 2:
            return f"Usage: {cc}channel <idx> on|off  or  {cc}channel sync"

        try:
            idx = int(args[0])
        except ValueError:
            return f"Invalid slot index: {args[0]!r} — must be an integer 0-7."

        action = args[1].lower()
        if action not in ("on", "off"):
            return f"Invalid action: {args[1]!r} — use 'on' or 'off'."

        row = await db.fetchone(
            "SELECT name, respond, disabled_at FROM _channels WHERE channel_idx=?", (idx,)
        )
        if row is None:
            return (
                f"Slot {idx} not found in channel table.\n"
                f"Run {cc}channel sync to enumerate from radio."
            )

        respond     = 1 if action == "on" else 0
        disabled_at = None  # on or off always clears safety-disable timestamp

        dispatcher.log_admin_attempt(
            f"{cc}channel {idx} {action}", msg, granted=True,
            reason=f"slot {idx} ({row['name']!r})"
        )

        await db.execute(
            "UPDATE _channels SET respond=?, disabled_at=? WHERE channel_idx=?",
            (respond, disabled_at, idx),
        )
        await db.commit()

        # Refresh connection manager's in-memory cache
        if _conn is not None:
            await _conn._reload_channel_cache()

        verb = "will now respond" if respond else "will no longer respond"
        return f"Slot {idx} ({row['name']!r}): {verb} in channel."

    dispatcher.register_admin_command(
        "!channel", cmd_channel,
        help_text="(Admin) Control channel respond flags or sync from radio",
        usage_text="!channel <idx> on|off  |  !channel sync",
        scope="direct",
        plugin_name="channels",
        category="channels",
    )

    # ── Rehash callback — re-enumerate on !rehash ─────────────────────────────

    async def on_rehash():
        if _conn is None:
            return None
        logger.info("Rehash: re-enumerating channel slots from radio.")
        return await _conn.enumerate_channels()

    dispatcher.register_rehash_callback(on_rehash)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(epoch: int) -> str:
    """Format an epoch timestamp as a compact local datetime string."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(epoch)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(epoch)
