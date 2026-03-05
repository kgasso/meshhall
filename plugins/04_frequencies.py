"""
Plugin: Frequency Directory
Commands:
  !freqs [category]  — List frequencies, optional category filter
  !freq <name>       — Look up a specific frequency entry
  !addfreq ...       — (Admin) Add/update a frequency entry
  !delfreq <name>    — (Admin) Remove a frequency entry
"""

__version__ = "0.2.1"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.
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


def setup(dispatcher, config, db):
    db.register_schema(SCHEMA)

    async def _seed():
        count = await db.fetchone("SELECT COUNT(*) as n FROM frequencies")
        if count and count["n"] > 0:
            return
        for entry in (config.plugin("frequencies").get("seed") or []):
            await db.execute(
                "INSERT OR IGNORE INTO frequencies (name,freq,mode,tone,category,notes,added_by) VALUES (?,?,?,?,?,?,?)",
                (entry.get("name","").upper(), entry.get("freq",""), entry.get("mode","FM"),
                 entry.get("tone",""), entry.get("category","general"), entry.get("notes",""), "config"),
            )
        await db.commit()

    seeded = {"done": False}

    async def seed_listener(msg):
        if not seeded["done"]:
            await _seed(); seeded["done"] = True

    dispatcher.register_listener(seed_listener)

    async def cmd_freqs(msg):
        await _seed(); seeded["done"] = True
        cat  = msg.arg_str.strip().lower()
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
        lines = []
        for cat_name, entries in by_cat.items():
            lines.append(f"[{cat_name.upper()}]")
            for e in entries:
                tone = f" T{e['tone']}" if e["tone"] else ""
                lines.append(f"  {e['name']}: {e['freq']} {e['mode']}{tone}")
        lines.append("Use !freq <name> for details.")
        return "\n".join(lines)

    dispatcher.register_command(
        "!freqs", cmd_freqs,
        help_text="Browse frequency directory",
        usage_text="!freqs [category]",
        scope="direct", priv_floor=PRIV_DEFAULT, category="frequencies", plugin_name="frequencies",
        allow_channel=True)

    async def cmd_freq(msg):
        name = msg.arg_str.strip().upper()
        if not name:
            return "Usage: !freq <name>"
        row = await db.fetchone("SELECT * FROM frequencies WHERE name=?", (name,))
        if not row:
            row = await db.fetchone("SELECT * FROM frequencies WHERE name LIKE ?", (f"%{name}%",))
        if not row:
            return f"'{name}' not found. Use !freqs to list all."
        tone  = f"\nTone: {row['tone']}" if row["tone"] else ""
        notes = f"\nNotes: {row['notes']}" if row["notes"] else ""
        return f"{row['name']}\nFreq: {row['freq']} MHz\nMode: {row['mode']}{tone}\nCat: {row['category']}{notes}"

    dispatcher.register_command(
        "!freq", cmd_freq,
        help_text="Look up a frequency by number",
        usage_text="!freq <n>",
        scope="direct", priv_floor=PRIV_DEFAULT, category="frequencies", plugin_name="frequencies",
        allow_channel=True)

    async def cmd_addfreq(msg):
        dispatcher.log_admin_attempt("!addfreq", msg, granted=True)
        parts = msg.arg_str.split()
        if len(parts) < 4:
            return "Usage: !addfreq <name> <freq> <mode> <category> [tone] [notes]"
        name, freq, mode, category = parts[0].upper(), parts[1], parts[2].upper(), parts[3].lower()
        tone  = parts[4] if len(parts) > 4 and not parts[4][0].isalpha() else ""
        notes = " ".join(parts[5 if tone else 4:])
        await db.execute(
            "INSERT OR REPLACE INTO frequencies (name,freq,mode,tone,category,notes,added_by) VALUES (?,?,?,?,?,?,?)",
            (name, freq, mode, tone, category, notes, msg.sender_id),
        )
        await db.commit()
        return f"Frequency {name} saved."

    dispatcher.register_admin_command(
        "!addfreq", cmd_addfreq,
        help_text="(Admin) Add a frequency",
        usage_text="!addfreq NAME FREQ MODE CATEGORY [TONE] [notes]",
        scope="direct", priv_floor=PRIV_ADMIN, category="frequencies", plugin_name="frequencies",
    )

    async def cmd_delfreq(msg):
        name = msg.arg_str.strip().upper()
        if not name:
            return "Usage: !delfreq <name>"
        row = await db.fetchone("SELECT name FROM frequencies WHERE name=?", (name,))
        if not row:
            return f"'{name}' not found."
        dispatcher.log_admin_attempt("!delfreq", msg, granted=True, reason=f"removing {name}")
        await db.execute("DELETE FROM frequencies WHERE name=?", (name,))
        await db.commit()
        return f"Frequency {name} removed."

    dispatcher.register_admin_command(
        "!delfreq", cmd_delfreq,
        help_text="(Admin) Remove a frequency",
        usage_text="!delfreq <n>",
        scope="direct", priv_floor=PRIV_ADMIN, category="frequencies", plugin_name="frequencies",
    )
