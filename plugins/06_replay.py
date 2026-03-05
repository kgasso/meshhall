"""
Plugin: Message Replay
Commands:
  !replay [n|Xh|Xd]  — Replay last N messages, or messages from past X hours/days
  !search <term>      — Search message history
"""

__version__ = "0.2.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.
import time
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def setup(dispatcher, config, db):

    async def cmd_replay(msg):
        max_replay = config.plugin("replay").get("max_messages", 100)
        arg = msg.arg_str.strip().lower()
        now = int(time.time())
        if not arg:
            limit, since = 20, None
        elif arg.endswith("h"):
            try:    since, limit = now - int(float(arg[:-1]) * 3600), max_replay
            except ValueError: return "Usage: !replay [n | Xh | Xd]"
        elif arg.endswith("d"):
            try:    since, limit = now - int(float(arg[:-1]) * 86400), max_replay
            except ValueError: return "Usage: !replay [n | Xh | Xd]"
        else:
            try:    limit, since = min(int(arg), max_replay), None
            except ValueError: return "Usage: !replay [n | Xh | Xd]"

        channel = msg.channel
        if since:
            rows = await db.fetchall(
                "SELECT ts,sender_name,sender_id,content FROM messages WHERE channel IS ? AND ts>=? AND sender_id!='bot' ORDER BY ts ASC LIMIT ?",
                (channel, since, limit),
            )
        else:
            rows = await db.fetchall(
                "SELECT ts,sender_name,sender_id,content FROM messages WHERE channel IS ? AND sender_id!='bot' ORDER BY ts DESC LIMIT ?",
                (channel, limit),
            )
            rows = list(reversed(rows))

        if not rows:
            return "No messages found for that range."
        lines = [f"Replay: {len(rows)} msgs" + (f" since {_fmt_ts(since)}" if since else ""), "---"]
        for r in rows:
            lines.append(f"[{_fmt_ts(r['ts'])}] {r['sender_name'] or r['sender_id']}: {r['content']}")
        return "\n".join(lines)

    dispatcher.register_command(
        "!replay", cmd_replay,
        help_text="Replay recent messages",
        usage_text="!replay [n | Xh | Xd]",
        scope="channel", priv_floor=PRIV_DEFAULT, category="utility", plugin_name="replay",
        allow_channel=True)

    async def cmd_search(msg):
        term = msg.arg_str.strip()
        if not term or len(term) < 3:
            return "Usage: !search <term> (min 3 chars)"
        rows = await db.fetchall(
            "SELECT ts,sender_name,sender_id,content FROM messages WHERE content LIKE ? AND channel IS ? ORDER BY ts DESC LIMIT 10",
            (f"%{term}%", msg.channel),
        )
        if not rows:
            return f"No messages found containing '{term}'."
        lines = [f"Search '{term}': {len(rows)} result(s)"]
        for r in rows:
            lines.append(f"[{_fmt_ts(r['ts'])}] {r['sender_name'] or r['sender_id']}: {r['content'][:100]}")
        return "\n".join(lines)

    dispatcher.register_command(
        "!search", cmd_search,
        help_text="Search message history by keyword",
        usage_text="!search <term>",
        scope="channel", priv_floor=PRIV_DEFAULT, category="utility", plugin_name="replay",
        allow_channel=True)
