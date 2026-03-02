"""
Plugin: Bulletin Board
Commands:
  !post <msg>      — Post a bulletin (privilege 2+)
  !bulletins [n]   — List last N bulletins
  !bulletin <id>   — Read a specific bulletin
  !delbul <id>     — Delete your own bulletin (or any if admin)
"""

__version__ = "0.2.1"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.
import time
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT, PRIV_ADMIN

SCHEMA = """
CREATE TABLE IF NOT EXISTS bulletins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    sender_id   TEXT NOT NULL,
    sender_name TEXT,
    content     TEXT NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0
);
"""


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    async def cmd_post(msg):
        content = msg.arg_str.strip()
        if not content:
            return "Usage: !post <message>"
        pcfg    = config.plugin("bulletin")
        max_len = pcfg.get("max_length", 500)
        if len(content) > max_len:
            return f"Bulletin too long (max {max_len} chars)."
        cur = await db.execute(
            "INSERT INTO bulletins (ts, sender_id, sender_name, content) VALUES (?,?,?,?)",
            (msg.ts, msg.sender_id, msg.sender_name, content),
        )
        await db.commit()
        return f"Bulletin #{cur.lastrowid} posted."

    dispatcher.register_command(
        "!post", cmd_post,
        help_text="Post a bulletin",
        usage_text="!post <message>",
        scope="direct", priv_floor=2, category="bulletin", plugin_name="bulletin",
    )

    async def cmd_bulletins(msg):
        try:
            n = min(int(msg.arg_str.strip()), 20) if msg.arg_str.strip() else 5
        except ValueError:
            n = 5
        rows = await db.fetchall(
            "SELECT id, ts, sender_name, sender_id, content FROM bulletins WHERE deleted=0 ORDER BY ts DESC LIMIT ?",
            (n,),
        )
        if not rows:
            return "No bulletins posted yet."
        lines = [f"Last {len(rows)} bulletin(s):"]
        for r in rows:
            name    = r["sender_name"] or r["sender_id"]
            preview = r["content"][:60] + ("…" if len(r["content"]) > 60 else "")
            lines.append(f"#{r['id']} [{_fmt_ts(r['ts'])}] {name}: {preview}")
        lines.append("Use !bulletin <id> to read full text.")
        return "\n".join(lines)

    dispatcher.register_command(
        "!bulletins", cmd_bulletins,
        help_text="List recent bulletins",
        usage_text="!bulletins [count]",
        scope="direct", priv_floor=PRIV_DEFAULT, category="bulletin", plugin_name="bulletin",
    )

    async def cmd_bulletin(msg):
        try:
            bul_id = int(msg.arg_str.strip())
        except (ValueError, TypeError):
            return "Usage: !bulletin <id>"
        row = await db.fetchone("SELECT * FROM bulletins WHERE id=? AND deleted=0", (bul_id,))
        if not row:
            return f"Bulletin #{bul_id} not found."
        name = row["sender_name"] or row["sender_id"]
        return f"Bulletin #{row['id']} [{_fmt_ts(row['ts'])}]\nFrom: {name}\n{row['content']}"

    dispatcher.register_command(
        "!bulletin", cmd_bulletin,
        help_text="Read a bulletin by ID",
        usage_text="!bulletin <id>",
        scope="direct", priv_floor=PRIV_DEFAULT, category="bulletin", plugin_name="bulletin",
    )

    async def cmd_delbul(msg):
        try:
            bul_id = int(msg.arg_str.strip())
        except (ValueError, TypeError):
            return "Usage: !delbul <id>"
        pcfg   = config.plugin("bulletin")
        admins = pcfg.get("admins") or config.get("bot.admins", [])
        row    = await db.fetchone(
            "SELECT sender_id FROM bulletins WHERE id=? AND deleted=0", (bul_id,)
        )
        if not row:
            return f"Bulletin #{bul_id} not found."
        if row["sender_id"] != msg.sender_id and msg.sender_id not in admins:
            dispatcher.log_admin_attempt("!delbul", msg, granted=False,
                                         reason=f"bulletin #{bul_id} owned by {row['sender_id']}")
            return "You can only delete your own bulletins."
        dispatcher.log_admin_attempt("!delbul", msg, granted=True, reason=f"deleting #{bul_id}")
        await db.execute("UPDATE bulletins SET deleted=1 WHERE id=?", (bul_id,))
        await db.commit()
        return f"Bulletin #{bul_id} deleted."

    dispatcher.register_command(
        "!delbul", cmd_delbul,
        help_text="Delete a bulletin by ID",
        usage_text="!delbul <id>",
        scope="direct", priv_floor=2, category="bulletin", plugin_name="bulletin",
    )
