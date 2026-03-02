"""
Plugin: User Registry & Privilege Management
Commands:
  !whoami              — (built-in dispatcher command, not registered here)
  !whois <name|id>     — Look up a user's registry record
  !users [filter]      — List known users with privilege levels
  !setpriv <id> <n>    — Set a user's privilege level (0-15)
  !mute <id|name>      — Set privilege to 0 (shorthand for !setpriv ... 0)
  !unmute <id|name>    — Restore privilege to 1 (shorthand for !setpriv ... 1)

Privilege levels:
  0  = muted    — all messages silently dropped
  1  = default  — standard read-only access (auto-assigned on first contact)
  2-14           = configurable tiers, set per-command in plugin configs
  15 = admin    — full access

Config: config/plugins/users.yaml
"""

__version__ = "0.1.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.

import time
from datetime import datetime, timezone
from core.database import PRIV_MUTED, PRIV_DEFAULT, PRIV_ADMIN


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%Mz")


def _fmt_age(ts: int) -> str:
    delta = int(time.time()) - ts
    if delta < 3600:   return f"{delta // 60}m ago"
    if delta < 86400:  return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _priv_label(priv: int) -> str:
    if priv == PRIV_MUTED:   return "muted"
    if priv == PRIV_DEFAULT: return "default"
    if priv == PRIV_ADMIN:   return "admin"
    return f"L{priv}"


def setup(dispatcher, config, db):

    async def cmd_whois(msg):
        query = msg.arg_str.strip()
        if not query:
            return "Usage: !whois <name or ID>"

        user = await db.find_user(query)
        if not user:
            return f"No user found matching '{query}'."

        name    = user["display_name"] or "(no name)"
        priv    = user["privilege"]
        seen    = _fmt_age(user["last_seen_ts"])
        joined  = _fmt_ts(user["first_seen_ts"])
        notes   = f"\nNotes: {user['notes']}" if user["notes"] else ""
        return (
            f"User: {name}\n"
            f"ID:   {user['pubkey_prefix']}\n"
            f"Priv: {priv} ({_priv_label(priv)})\n"
            f"Last seen: {seen}\n"
            f"First seen: {joined}"
            f"{notes}"
        )

    dispatcher.register_command(
        "!whois", cmd_whois,
        help_text="Look up a user by name or ID",
        usage_text="!whois <name or ID>",
        scope="direct",
        priv_floor=PRIV_DEFAULT, category="users", plugin_name="users",
    )

    async def cmd_users(msg):
        pcfg     = config.plugin("users")
        max_list = pcfg.get("max_list", 30)
        query    = msg.arg_str.strip()

        if query:
            rows = await db.fetchall(
                """SELECT * FROM users
                   WHERE LOWER(display_name) LIKE LOWER(?)
                      OR pubkey_prefix LIKE ?
                   ORDER BY last_seen_ts DESC LIMIT ?""",
                (f"%{query}%", f"{query}%", max_list),
            )
        else:
            rows = await db.fetchall(
                "SELECT * FROM users ORDER BY last_seen_ts DESC LIMIT ?",
                (max_list,),
            )

        if not rows:
            return "No users in registry yet." if not query else f"No users matching '{query}'."

        lines = [f"Users ({len(rows)}{'/' + str(max_list) if len(rows) == max_list else ''}):"]
        for u in rows:
            name = u["display_name"] or "(unnamed)"
            lines.append(
                f"  {name} ({u['pubkey_prefix']}) "
                f"L{u['privilege']} — {_fmt_age(u['last_seen_ts'])}"
            )
        return "\n".join(lines)

    dispatcher.register_command(
        "!users", cmd_users,
        help_text="List known users",
        usage_text="!users [filter]",
        scope="direct",
        priv_floor=PRIV_ADMIN, category="users", plugin_name="users",
    )

    async def cmd_setpriv(msg):
        parts = msg.arg_str.strip().split()
        if len(parts) < 2:
            return "Usage: !setpriv <id or name> <0-15>"
        try:
            new_priv = int(parts[-1])
        except ValueError:
            return "Privilege must be a number 0-15."
        if not 0 <= new_priv <= 15:
            return "Privilege must be 0-15."

        query = " ".join(parts[:-1])
        user  = await db.find_user(query)
        if not user:
            return f"No user found matching '{query}'."

        # Prevent self-demotion from admin — safety rail
        if user["pubkey_prefix"] == msg.sender_id and new_priv < PRIV_ADMIN:
            return "You cannot reduce your own admin privilege."

        old_priv = user["privilege"]
        await db.set_privilege(user["pubkey_prefix"], new_priv)

        name = user["display_name"] or user["pubkey_prefix"]
        dispatcher.log_admin_attempt(
            "!setpriv", msg, granted=True,
            reason=f"{name} {old_priv} → {new_priv} ({_priv_label(new_priv)})"
        )
        return (
            f"Privilege updated: {name} ({user['pubkey_prefix']})\n"
            f"{old_priv} ({_priv_label(old_priv)}) → "
            f"{new_priv} ({_priv_label(new_priv)})"
        )

    dispatcher.register_admin_command(
        "!setpriv", cmd_setpriv,
        help_text="(Admin) Set a user's privilege level",
        usage_text="!setpriv <id or name> <0-15>",
        scope="direct",
        priv_floor=PRIV_ADMIN, category="users", plugin_name="users",
    )

    async def cmd_mute(msg):
        query = msg.arg_str.strip()
        if not query:
            return "Usage: !mute <id or name>"
        user = await db.find_user(query)
        if not user:
            return f"No user found matching '{query}'."
        if user["pubkey_prefix"] == msg.sender_id:
            return "You cannot mute yourself."

        name = user["display_name"] or user["pubkey_prefix"]
        await db.set_privilege(user["pubkey_prefix"], PRIV_MUTED)
        dispatcher.log_admin_attempt(
            "!mute", msg, granted=True, reason=f"muted {name} ({user['pubkey_prefix']})"
        )
        return f"Muted: {name} ({user['pubkey_prefix']}). All their messages will be silently dropped."

    dispatcher.register_admin_command(
        "!mute", cmd_mute,
        help_text="(Admin) Mute a user (sets privilege 0)",
        usage_text="!mute <id or name>",
        scope="direct",
        priv_floor=PRIV_ADMIN, category="users", plugin_name="users",
    )

    async def cmd_unmute(msg):
        query = msg.arg_str.strip()
        if not query:
            return "Usage: !unmute <id or name>"
        user = await db.find_user(query)
        if not user:
            return f"No user found matching '{query}'."

        name = user["display_name"] or user["pubkey_prefix"]
        if user["privilege"] != PRIV_MUTED:
            return f"{name} is not muted (privilege is {user['privilege']})."

        await db.set_privilege(user["pubkey_prefix"], PRIV_DEFAULT)
        dispatcher.log_admin_attempt(
            "!unmute", msg, granted=True,
            reason=f"restored {name} ({user['pubkey_prefix']}) to default"
        )
        return f"Unmuted: {name}. Privilege restored to {PRIV_DEFAULT} (default)."

    dispatcher.register_admin_command(
        "!unmute", cmd_unmute,
        help_text="(Admin) Restore a user from mute",
        usage_text="!unmute <id or name>",
        scope="direct",
        priv_floor=PRIV_ADMIN, category="users", plugin_name="users",
    )
