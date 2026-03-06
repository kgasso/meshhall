import re
"""
Plugin: Frequency Directory
Commands:
  !freq <subcommand>      — Frequency directory management
  !freqs [category]       — Shortcut for !freq list [category]

Subcommands:
  !freq list [category]   — List frequencies, optional category filter
  !freq show <name>       — Look up a specific frequency entry
  !freq add ...           — (Admin) Add/update a frequency entry
  !freq delete <name>     — (Admin) Remove a frequency entry
"""

__version__ = "0.4.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

from core.database import PRIV_DEFAULT, PRIV_ADMIN

SCHEMA = """
CREATE TABLE IF NOT EXISTS frequencies (
    name        TEXT PRIMARY KEY,
    freq        TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'FM',
    tone        TEXT,
    category    TEXT NOT NULL DEFAULT 'general',
    notes       TEXT,
    added_by    TEXT
);
"""


def _is_tone(s: str) -> bool:
    """
    Return True if the string looks like a squelch tone, not the start of notes.
    Matches:
      CTCSS  — numeric, e.g. 100.0  88.5  127.3
      DPL/DCS — D + 3 digits + optional N or I, e.g. D023N  D803N  D503I
      Silence — 0 / none / off  (caller normalises to empty string)
    """
    return bool(re.match(r'^(\d+\.?\d*|D\d{3}[NI]?)$', s, re.IGNORECASE))


def _normalise_tone(s: str) -> str:
    """Return empty string for 'no tone' sentinels, otherwise return uppercased."""
    if s.lower() in ("0", "none", "off"):
        return ""
    return s.upper()


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    async def _seed():
        count = await db.fetchone("SELECT COUNT(*) as n FROM frequencies")
        if count and count["n"] > 0:
            return
        for entry in (config.plugin("frequencies").get("seed") or []):
            await db.execute(
                "INSERT OR IGNORE INTO frequencies "
                "(name,freq,mode,tone,category,notes,added_by) VALUES (?,?,?,?,?,?,?)",
                (entry.get("name", "").upper(), entry.get("freq", ""),
                 entry.get("mode", "FM"), entry.get("tone", ""),
                 entry.get("category", "general"), entry.get("notes", ""), "config"),
            )
        await db.commit()

    seeded = {"done": False}

    async def seed_listener(msg):
        if not seeded["done"]:
            await _seed()
            seeded["done"] = True

    dispatcher.register_listener(seed_listener)

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def do_list(msg, args=""):
        await _seed()
        seeded["done"] = True
        cat  = args.strip().lower()
        rows = await db.fetchall(
            "SELECT * FROM frequencies WHERE LOWER(category)=? ORDER BY name" if cat
            else "SELECT * FROM frequencies ORDER BY category, name",
            (cat,) if cat else (),
        )
        if not rows:
            return f"No frequencies{' in ' + cat if cat else ''}."
        by_cat: dict = {}
        for r in rows:
            by_cat.setdefault(r["category"], []).append(r)
        cc    = dispatcher.command_char
        lines = []
        for cat_name, entries in by_cat.items():
            lines.append(f"[{cat_name.upper()}]")
            for e in entries:
                tone = f" T{e['tone']}" if e["tone"] else ""
                lines.append(f"  {e['name']}: {e['freq']} {e['mode']}{tone}")
        lines.append(f"Use {cc}freq show <name> for details.")
        return "\n".join(lines)

    async def do_show(msg, args=""):
        name = args.strip().upper()
        if not name:
            return "Usage: !freq show <name>"
        row = await db.fetchone("SELECT * FROM frequencies WHERE name=?", (name,))
        if not row:
            row = await db.fetchone(
                "SELECT * FROM frequencies WHERE name LIKE ?", (f"%{name}%",)
            )
        if not row:
            cc = dispatcher.command_char
            return f"'{name}' not found. Use {cc}freq list to browse."
        tone  = f"\nTone: {row['tone']}" if row["tone"] else ""
        notes = f"\nNotes: {row['notes']}" if row["notes"] else ""
        return (
            f"{row['name']}\nFreq: {row['freq']} MHz\n"
            f"Mode: {row['mode']}{tone}\nCat: {row['category']}{notes}"
        )

    async def do_add(msg, args=""):
        dispatcher.log_admin_attempt("!freq add", msg, granted=True)
        parts = args.split()
        if len(parts) < 4:
            return "Usage: !freq add <name> <freq> <mode> <category> [tone] [notes]"
        name, freq, mode, category = (
            parts[0].upper(), parts[1], parts[2].upper(), parts[3].lower()
        )
        raw_tone = parts[4] if len(parts) > 4 and _is_tone(parts[4]) else ""
        tone     = _normalise_tone(raw_tone) if raw_tone else ""
        notes    = " ".join(parts[5 if raw_tone else 4:])
        await db.execute(
            "INSERT OR REPLACE INTO frequencies "
            "(name,freq,mode,tone,category,notes,added_by) VALUES (?,?,?,?,?,?,?)",
            (name, freq, mode, tone, category, notes, msg.sender_id),
        )
        await db.commit()
        return f"Frequency {name} saved."

    async def do_delete(msg, args=""):
        name = args.strip().upper()
        if not name:
            return "Usage: !freq delete <name>"
        row = await db.fetchone("SELECT name FROM frequencies WHERE name=?", (name,))
        if not row:
            return f"'{name}' not found."
        dispatcher.log_admin_attempt("!freq delete", msg, granted=True,
                                     reason=f"removing {name}")
        await db.execute("DELETE FROM frequencies WHERE name=?", (name,))
        await db.commit()
        return f"Frequency {name} removed."

    _SUBCOMMANDS = {
        "list":   do_list,
        "show":   do_show,
        "add":    do_add,
        "delete": do_delete,
    }
    _ADMIN_SUBS = {"add", "delete"}

    # ── Subcommand dispatcher ─────────────────────────────────────────────────

    async def cmd_freq(msg, args=""):
        parts = (args or msg.arg_str).strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""

        if not sub:
            cc = dispatcher.command_char
            return (
                f"Frequency commands:\n"
                f"  {cc}freq list [category]          — browse directory\n"
                f"  {cc}freq show <name>              — look up a frequency\n"
                f"  {cc}freq add <n> <f> <m> <cat>   — add/update entry (admin)\n"
                f"  {cc}freq delete <name>            — remove entry (admin)"
            )

        handler = _SUBCOMMANDS.get(sub)
        if not handler:
            cc = dispatcher.command_char
            return f"Unknown subcommand '{sub}'. Use {cc}freq for the list."

        if sub in _ADMIN_SUBS:
            privilege = await db.get_privilege(msg.sender_id)
            if privilege < PRIV_ADMIN:
                return f"Access denied. !freq {sub} requires admin privilege."

        sub_args = parts[1] if len(parts) > 1 else ""
        return await handler(msg, sub_args)

    dispatcher.register_command(
        "!freq", cmd_freq,
        help_text="Frequency directory — browse and manage frequencies",
        usage_text="!freq <list|show|add|delete> [args]",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="frequencies", plugin_name="frequencies", allow_channel=True,
    )

    # ── Standalone shortcut ───────────────────────────────────────────────────

    async def cmd_freqs_shortcut(msg):
        return await cmd_freq(msg, ("list " + msg.arg_str).strip())

    dispatcher.register_command(
        "!freqs", cmd_freqs_shortcut,
        help_text="Browse frequency directory (shortcut for !freq list)",
        is_shortcut=True,
        usage_text="!freqs [category]",
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="frequencies", plugin_name="frequencies", allow_channel=True,
    )
