"""
Plugin Template — copy this to plugins/XX_myplugin.py to create a new plugin.

Naming convention:
  Prefix with a two-digit number to control load order.
  Prefix with underscore to disable without deleting: _disabled_plugin.py

Every plugin exposes a single setup() function. That's the only contract.
"""

# ── Optional: define a DB schema for this plugin ──────────────────────────────
# Tables are created automatically before the bot starts taking messages.
SCHEMA = """
CREATE TABLE IF NOT EXISTS my_plugin_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    sender_id   TEXT NOT NULL,
    value       TEXT
);
"""


def setup(dispatcher, config, db):
    """
    Called once at startup. Register commands, listeners, and schemas here.

    Args:
        dispatcher: Core event bus. Use to register commands and listeners.
        config:     Config object. Use config.plugin("name") for plugin config.
        db:         Database object. Use await db.fetchone(), db.execute(), etc.
    """

    # Register your DB schema (idempotent — safe to call every startup)
    db.register_schema(SCHEMA)

    # Load plugin-specific config from config/plugins/myplugin.yaml
    # If the file doesn't exist, pcfg silently returns defaults — no errors.
    # This means a config file for a disabled plugin causes no problems.
    pcfg = config.plugin("myplugin")   # matches config/plugins/myplugin.yaml
    my_setting = pcfg.get("some_setting", "default_value")

    # You can also fall back to global bot config for shared settings like admins:
    admins = pcfg.get("admins") or config.get("bot.admins", [])

    # ── Register a command ────────────────────────────────────────────────────
    async def cmd_example(msg):
        """
        msg.sender_id   — unique node ID of sender
        msg.sender_name — display name (may be None)
        msg.content     — full message text
        msg.command     — "!example"
        msg.arg_str     — everything after the command
        msg.channel     — channel name, or None if DM
        msg.is_dm       — True if direct message
        msg.ts          — Unix timestamp

        Return a string to reply, or None for no reply.
        Reply is automatically chunked into mesh-sized packets.
        """
        name = msg.sender_name or msg.sender_id
        return f"Hello, {name}! You said: {msg.arg_str or '(nothing)'}"

    dispatcher.register_command(
        "!example",
        cmd_example,
        help_text="Example command. Usage: !example [text]",
        allow_channel=False,   # True = also responds in channels, not just DMs
    )

    # ── Register a passive listener ────────────────────────────────────────────
    # Listeners receive every message regardless of whether it's a command.
    # Use for logging, monitoring, alerting, store-and-forward, etc.
    # Return value is ignored.
    async def my_listener(msg):
        # Example: log every message that mentions a keyword
        if "emergency" in msg.content.lower():
            await db.execute(
                "INSERT INTO my_plugin_data (ts, sender_id, value) VALUES (?,?,?)",
                (msg.ts, msg.sender_id, msg.content),
            )
            await db.commit()

    dispatcher.register_listener(my_listener)
