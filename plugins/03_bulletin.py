"""
Plugin: Bulletin Board
Commands:
  !bulletin <subcommand>  — Bulletin board management
  !post <msg>             — Shortcut for !bulletin post
  !bulletins [n]          — Shortcut for !bulletin list

Subcommands:
  !bulletin list [n]      — List last N bulletins (default 5)
  !bulletin show <id>     — Read a specific bulletin
  !bulletin post [msg]    — Post inline or finalize pending draft
  !bulletin draft <text>  — Start or append to a pending draft
  !bulletin draft clear   — Discard pending draft
  !bulletin delete <id>   — Delete a bulletin (own or any if admin)
"""

__version__ = "0.4.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

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

CREATE TABLE IF NOT EXISTS bulletin_drafts (
    pubkey_prefix TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    updated_ts    INTEGER NOT NULL
);
"""


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _fmt_sender(row):
    """Return 'Name (id)' or just 'id' if no name stored."""
    name = row["sender_name"]
    pk   = row["sender_id"]
    return f"{name} ({pk})" if name else pk


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def do_list(msg, args=""):
        try:
            n = min(int(args.strip()), 20) if args.strip() else 5
        except ValueError:
            n = 5
        rows = await db.fetchall(
            "SELECT id, ts, sender_name, sender_id, content "
            "FROM bulletins WHERE deleted=0 ORDER BY ts DESC LIMIT ?",
            (n,),
        )
        if not rows:
            return "No bulletins posted yet."
        cc    = dispatcher.command_char
        lines = [f"Last {len(rows)} bulletin(s):"]
        for r in rows:
            sender  = _fmt_sender(r)
            preview = r["content"][:60] + ("…" if len(r["content"]) > 60 else "")
            lines.append(f"#{r['id']} [{_fmt_ts(r['ts'])}] {sender}: {preview}")
        lines.append(f"Use {cc}bulletin show <id> to read full text.")
        return "\n".join(lines)

    async def do_show(msg, args=""):
        try:
            bul_id = int(args.strip())
        except (ValueError, TypeError):
            return "Usage: !bulletin show <id>"
        row = await db.fetchone(
            "SELECT * FROM bulletins WHERE id=? AND deleted=0", (bul_id,)
        )
        if not row:
            return f"Bulletin #{bul_id} not found."
        return (
            f"Bulletin #{row['id']} [{_fmt_ts(row['ts'])}]\n"
            f"From: {_fmt_sender(row)}\n"
            f"{row['content']}"
        )

    async def do_post(msg, args=""):
        content = args.strip()
        pcfg    = config.plugin("bulletin")
        max_len = pcfg.get("max_length", 2000)

        # No inline content — check for pending draft
        if not content:
            draft = await db.fetchone(
                "SELECT content FROM bulletin_drafts WHERE pubkey_prefix=?",
                (msg.sender_id,),
            )
            if not draft:
                return "No draft pending and no message provided. Use !bulletin post <text> or !bulletin draft <text> first."
            content = draft["content"]
            await db.execute(
                "DELETE FROM bulletin_drafts WHERE pubkey_prefix=?",
                (msg.sender_id,),
            )
            source = "draft"
        else:
            if len(content) > max_len:
                return f"Bulletin too long (max {max_len} chars). Use !bulletin draft to build it up."
            source = "inline"

        cur = await db.execute(
            "INSERT INTO bulletins (ts, sender_id, sender_name, content) VALUES (?,?,?,?)",
            (int(time.time()), msg.sender_id, msg.sender_name, content),
        )
        await db.commit()
        return f"Bulletin #{cur.lastrowid} posted ({source}, {len(content)} chars)."

    async def do_draft(msg, args=""):
        text    = args.strip()
        cc      = dispatcher.command_char
        pcfg    = config.plugin("bulletin")
        max_len = pcfg.get("max_length", 2000)

        if not text:
            # Show current draft status
            draft = await db.fetchone(
                "SELECT content, updated_ts FROM bulletin_drafts WHERE pubkey_prefix=?",
                (msg.sender_id,),
            )
            if not draft:
                return f"No draft in progress. Use {cc}bulletin draft <text> to start one."
            return (
                f"Draft in progress ({len(draft['content'])} chars, "
                f"last updated {_fmt_ts(draft['updated_ts'])}):\n"
                f"{draft['content'][:120]}{'…' if len(draft['content']) > 120 else ''}\n"
                f"Use {cc}bulletin post to publish or {cc}bulletin draft clear to discard."
            )

        if text.lower() == "clear":
            await db.execute(
                "DELETE FROM bulletin_drafts WHERE pubkey_prefix=?",
                (msg.sender_id,),
            )
            await db.commit()
            return "Draft discarded."

        # Append to existing draft or start new one
        existing = await db.fetchone(
            "SELECT content FROM bulletin_drafts WHERE pubkey_prefix=?",
            (msg.sender_id,),
        )
        if existing:
            new_content = existing["content"] + " " + text
        else:
            new_content = text

        if len(new_content) > max_len:
            return (
                f"Draft would exceed max length ({max_len} chars). "
                f"Current draft is {len(existing['content']) if existing else 0} chars. "
                f"Use {cc}bulletin post to publish what you have, or {cc}bulletin draft clear to start over."
            )

        await db.execute(
            """INSERT INTO bulletin_drafts (pubkey_prefix, content, updated_ts)
               VALUES (?,?,?)
               ON CONFLICT(pubkey_prefix) DO UPDATE SET content=excluded.content, updated_ts=excluded.updated_ts""",
            (msg.sender_id, new_content, int(time.time())),
        )
        await db.commit()
        return (
            f"Draft updated ({len(new_content)} chars). "
            f"Use {cc}bulletin draft <more text> to keep adding, "
            f"or {cc}bulletin post to publish."
        )

    async def do_delete(msg, args=""):
        try:
            bul_id = int(args.strip())
        except (ValueError, TypeError):
            return "Usage: !bulletin delete <id>"
        pcfg   = config.plugin("bulletin")
        admins = pcfg.get("admins") or config.get("bot.admins", [])
        row    = await db.fetchone(
            "SELECT sender_id FROM bulletins WHERE id=? AND deleted=0", (bul_id,)
        )
        if not row:
            return f"Bulletin #{bul_id} not found."
        if row["sender_id"] != msg.sender_id and msg.sender_id not in admins:
            dispatcher.log_admin_attempt("!bulletin delete", msg, granted=False,
                                         reason=f"bulletin #{bul_id} owned by {row['sender_id']}")
            return "You can only delete your own bulletins."
        dispatcher.log_admin_attempt("!bulletin delete", msg, granted=True,
                                     reason=f"deleting #{bul_id}")
        await db.execute("UPDATE bulletins SET deleted=1 WHERE id=?", (bul_id,))
        await db.commit()
        return f"Bulletin #{bul_id} deleted."

    _SUBCOMMANDS = {
        "list":   do_list,
        "show":   do_show,
        "post":   do_post,
        "draft":  do_draft,
        "delete": do_delete,
    }

    # ── Subcommand dispatcher ─────────────────────────────────────────────────

    async def cmd_bulletin(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc = dispatcher.command_char
            return (
                f"Bulletin commands:\n"
                f"  {cc}bulletin list [n]        — list recent bulletins\n"
                f"  {cc}bulletin show <id>       — read a bulletin\n"
                f"  {cc}bulletin post [msg]      — post inline or finalize draft (shortcut: {cc}post)\n"
                f"  {cc}bulletin draft <text>    — start or append to a draft\n"
                f"  {cc}bulletin draft clear     — discard draft\n"
                f"  {cc}bulletin delete <id>     — delete a bulletin\n"
                f"  {cc}bulletins [n]            — shortcut for list"
            )

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}bulletin for the list."

        # post and draft require priv 2+
        if sub in ("post", "draft"):
            privilege = await db.get_privilege(msg.sender_id)
            if privilege < 2:
                return "Posting bulletins requires privilege 2 or higher."

        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_command(
        "!bulletin", cmd_bulletin,
        help_text="Bulletin board — post, list, read, draft, and delete bulletins",
        usage_text="!bulletin <list|show|post|draft|delete> [args]",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="bulletin", plugin_name="bulletin", allow_channel=True,
    )

    # ── Standalone shortcuts ──────────────────────────────────────────────────

    async def cmd_post_shortcut(msg):
        return await cmd_bulletin(msg, ("post " + msg.arg_str).strip())

    async def cmd_bulletins_shortcut(msg):
        return await cmd_bulletin(msg, ("list " + msg.arg_str).strip())

    dispatcher.register_command(
        "!post", cmd_post_shortcut,
        help_text="Post a bulletin (shortcut for !bulletin post)",
        is_shortcut=True,
        usage_text="!post <message>  — use !bulletin post for full usage",
        scope="direct", priv_floor=2,
        category="bulletin", plugin_name="bulletin",
    )
    dispatcher.register_command(
        "!bulletins", cmd_bulletins_shortcut,
        help_text="List recent bulletins (shortcut for !bulletin list)",
        is_shortcut=True,
        usage_text="!bulletins [count]  — use !bulletin list for full usage",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="bulletin", plugin_name="bulletin", allow_channel=True,
    )
