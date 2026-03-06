"""
Plugin: Message Replay
Commands:
  !replay <subcommand>        — Message history commands
  !search <term>              — Shortcut for !replay search

Subcommands:
  !replay list [n|Xh|Xd]     — Replay last N messages or messages from past X hours/days
  !replay search <term>       — Search message history by keyword
"""

__version__ = "0.4.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import time
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


async def _resolve_name(db, sender_id: str, sender_name) -> str:
    """Return best available display name, falling back to DB lookup then ID."""
    if sender_name:
        return sender_name
    user = await db.get_user(sender_id)
    if user and user.get("display_name"):
        name = user["display_name"]
        return f"{name} ({sender_id})"
    return sender_id


def setup(dispatcher, config, db):

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def do_list(msg, args=""):
        max_replay = config.plugin("replay").get("max_messages", 100)
        arg        = args.strip().lower()
        now        = int(time.time())

        if not arg:
            limit, since = 20, None
        elif arg.endswith("h"):
            try:
                since, limit = now - int(float(arg[:-1]) * 3600), max_replay
            except ValueError:
                return "Usage: !replay list [n | Xh | Xd]"
        elif arg.endswith("d"):
            try:
                since, limit = now - int(float(arg[:-1]) * 86400), max_replay
            except ValueError:
                return "Usage: !replay list [n | Xh | Xd]"
        else:
            try:
                limit, since = min(int(arg), max_replay), None
            except ValueError:
                return "Usage: !replay list [n | Xh | Xd]"

        channel = msg.channel
        if since:
            rows = await db.fetchall(
                "SELECT ts,sender_name,sender_id,content FROM messages "
                "WHERE channel IS ? AND ts>=? AND sender_id!='bot' "
                "ORDER BY ts ASC LIMIT ?",
                (channel, since, limit),
            )
        else:
            rows = await db.fetchall(
                "SELECT ts,sender_name,sender_id,content FROM messages "
                "WHERE channel IS ? AND sender_id!='bot' "
                "ORDER BY ts DESC LIMIT ?",
                (channel, limit),
            )
            rows = list(reversed(rows))

        if not rows:
            return "No messages found for that range."
        lines = [
            f"Replay: {len(rows)} msgs"
            + (f" since {_fmt_ts(since)}" if since else ""),
            "---",
        ]
        for r in rows:
            name = await _resolve_name(db, r['sender_id'], r['sender_name'])
            lines.append(f"[{_fmt_ts(r['ts'])}] {name}: {r['content']}")
        return "\n".join(lines)

    async def do_search(msg, args=""):
        term = args.strip()
        if not term or len(term) < 3:
            return "Usage: !replay search <term> (min 3 chars)"
        rows = await db.fetchall(
            "SELECT ts,sender_name,sender_id,content FROM messages "
            "WHERE content LIKE ? AND channel IS ? ORDER BY ts DESC LIMIT 10",
            (f"%{term}%", msg.channel),
        )
        if not rows:
            return f"No messages found containing '{term}'."
        lines = [f"Search '{term}': {len(rows)} result(s)"]
        for r in rows:
            name = await _resolve_name(db, r['sender_id'], r['sender_name'])
            lines.append(f"[{_fmt_ts(r['ts'])}] {name}: {r['content'][:100]}")
        return "\n".join(lines)

    _SUBCOMMANDS = {
        "list":   do_list,
        "search": do_search,
    }

    # ── Subcommand dispatcher ─────────────────────────────────────────────────

    async def cmd_replay(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc = dispatcher.command_char
            return (
                f"Replay commands:\n"
                f"  {cc}replay list [n|Xh|Xd]   — show recent messages\n"
                f"  {cc}replay search <term>     — search message history"
            )

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}replay for the list."

        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_command(
        "!replay", cmd_replay,
        help_text="Message history — replay and search recent messages",
        usage_text="!replay <list|search> [args]",
        scope="channel", priv_floor=PRIV_DEFAULT,
        category="utility", plugin_name="replay", allow_channel=True,
    )

    # ── Standalone shortcut ───────────────────────────────────────────────────

    async def cmd_search_shortcut(msg):
        return await cmd_replay(msg, ("search " + msg.arg_str).strip())

    dispatcher.register_command(
        "!search", cmd_search_shortcut,
        help_text="Search message history (shortcut for !replay search)",
        is_shortcut=True,
        usage_text="!search <term>",
        scope="channel", priv_floor=PRIV_DEFAULT,
        category="utility", plugin_name="replay", allow_channel=True,
    )
