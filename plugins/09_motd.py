"""
Plugin: Message of the Day (MOTD)
Commands:
  !motd            — Show the current message of the day
  !setmotd <text>  — Set the message of the day (admin)
  !clearmotd       — Clear the message of the day (admin)

The MOTD is delivered automatically after the welcome message on a user's first
DM (or when the intro window elapses), if one is set. It is also available
on-demand via !motd.

Config: config/plugins/motd.yaml
"""

__version__ = "0.1.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import logging
import time
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT, PRIV_ADMIN

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS motd (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    text    TEXT NOT NULL,
    set_by  TEXT,
    set_ts  INTEGER NOT NULL
);
"""


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    def _cfg():
        return config.plugin("motd")

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _get_motd() -> dict | None:
        """Return the current MOTD row, or None if not set."""
        return await db.fetchone("SELECT text, set_by, set_ts FROM motd WHERE id=1")

    async def _set_motd(text: str, sender_id: str):
        await db.execute(
            """INSERT INTO motd (id, text, set_by, set_ts) VALUES (1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET text=excluded.text,
               set_by=excluded.set_by, set_ts=excluded.set_ts""",
            (text, sender_id, int(time.time())),
        )
        await db.commit()

    async def _clear_motd():
        await db.execute("DELETE FROM motd WHERE id=1")
        await db.commit()

    # ── MOTD delivery hook ────────────────────────────────────────────────────
    # The dispatcher fires _maybe_send_welcome, then any post-welcome hooks.
    # We register a rehash callback that also exposes a callable for the
    # dispatcher to invoke after welcome — achieved via a listener that checks
    # whether this is a fresh welcome situation and appends the MOTD.
    #
    # Simpler approach: register a listener that piggybacks on the welcome flow
    # by checking welcomed_ts freshness. When a user was just welcomed (within
    # the last 5 seconds), queue the MOTD as a follow-up DM.

    async def motd_delivery_listener(msg):
        """After a welcome fires, deliver the MOTD to the same user if set."""
        if msg.channel:
            return  # DMs only
        motd_row = await _get_motd()
        if not motd_row:
            return
        user = await db.get_user(msg.sender_id)
        if not user or not user["welcomed_ts"]:
            return
        # Only deliver if the welcome just fired (within last 10s).
        if int(time.time()) - user["welcomed_ts"] > 10:
            return
        # Route through dispatcher.enqueue_dm so text is chunked correctly
        # at the 156-byte firmware limit rather than bypassing it.
        await dispatcher.enqueue_dm(msg.sender_id, f"📢 MOTD: {motd_row['text']}")

    dispatcher.register_listener(motd_delivery_listener)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_motd(msg):
        motd_row = await _get_motd()
        if not motd_row:
            return "No message of the day is set."
        set_info = f"\n(Set {_fmt_ts(motd_row['set_ts'])})"
        return f"📢 MOTD: {motd_row['text']}{set_info}"

    dispatcher.register_command(
        "!motd", cmd_motd,
        help_text="Show the current message of the day",
        scope="direct", priv_floor=PRIV_DEFAULT, category="motd", plugin_name="motd",
    )

    async def cmd_setmotd(msg):
        text = msg.arg_str.strip()
        if not text:
            return "Usage: !setmotd <message text>"
        max_len = _cfg().get("max_length", 200)
        if len(text) > max_len:
            return f"MOTD too long — max {max_len} characters (yours: {len(text)})."
        await _set_motd(text, msg.sender_id)
        dispatcher.log_admin_attempt("!setmotd", msg, granted=True,
                                     reason=f"set MOTD: {text[:60]}{'…' if len(text) > 60 else ''}")
        return f"MOTD set ({len(text)} chars). Users will see it on next contact."

    dispatcher.register_admin_command(
        "!setmotd", cmd_setmotd,
        help_text="(Admin) Set the message of the day",
        usage_text="!setmotd <message text>",
        scope="direct", priv_floor=PRIV_ADMIN, category="motd", plugin_name="motd",
    )

    async def cmd_clearmotd(msg):
        motd_row = await _get_motd()
        if not motd_row:
            return "No MOTD is currently set."
        await _clear_motd()
        dispatcher.log_admin_attempt("!clearmotd", msg, granted=True, reason="cleared MOTD")
        return "MOTD cleared."

    dispatcher.register_admin_command(
        "!clearmotd", cmd_clearmotd,
        help_text="(Admin) Clear the message of the day",
        scope="direct", priv_floor=PRIV_ADMIN, category="motd", plugin_name="motd",
    )
