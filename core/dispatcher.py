"""
Dispatcher — the central event bus.

Message flow:
  raw packet → ConnectionManager → Dispatcher.handle() →
      upsert user registry (skipped for sender_id="unknown") →
      check mute → match command →
      check scope → resolve privilege → check privilege → execute

Privilege levels (0-15):
  0  = muted    — all messages silently dropped
  1  = default  — auto-assigned on first contact
  2-14           = configurable tiers
  15 = admin    — full access

Privilege resolution (per command, at dispatch time):
  1. Plugin config:  config.plugin(plugin).get("privileges.<cmd>")
  2. Hardcoded floor in register_command() — config can only raise, never lower
  3. Clamped to [floor, 15]
  This means !rehash picks up privilege changes immediately.

Command scope (default hardcoded per command, operator-configurable via plugin YAML):
  "channel"  — DM or any channel the bot is in
  "direct"   — DM only
  "disabled" — silently dropped at dispatch; hidden from !help output

  Scope resolution (per command, at dispatch time):
    1. Plugin config: config.plugin(plugin_name).get("scopes.<cmd_key>")
    2. Hardcoded default in register_command()
  Restriction rules (widest → narrowest):
    - Any command can be disabled via config — no opt-in required.
    - "channel" can be tightened to "direct" or "disabled" via config.
    - "direct" can be tightened to "disabled" via config, or widened to
      "channel" only if allow_channel=True was passed at registration.
    - Built-in commands (plugin_name="") ignore config and use their default."""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Dict, List, Optional, NamedTuple

from core.database import PRIV_MUTED, PRIV_DEFAULT, PRIV_ADMIN
from core.ratelimit import ChannelRateLimiter, RateLimitResult

logger = logging.getLogger(__name__)

MAX_CHUNK = 156  # MeshCore firmware hard limit in UTF-8 bytes (confirmed empirically)


@dataclass
class Message:
    sender_id:   str
    sender_name: Optional[str]
    content:     str
    channel:     Optional[str]   # None = DM
    ts:          int = field(default_factory=lambda: int(time.time()))
    raw:         dict = field(default_factory=dict)

    @property
    def is_dm(self) -> bool:
        return self.channel is None

    @property
    def args(self) -> List[str]:
        return self.content.strip().split()

    def get_command(self, command_char: str = "!") -> Optional[str]:
        """
        Return the command token if the message starts with command_char.
        Tolerates a space between the command char and the command name —
        e.g. "! ping" is treated the same as "!ping" for mobile autocorrect QoL.
        """
        parts = self.args
        if not parts:
            return None
        first = parts[0]
        # Standard: "!ping"
        if first.startswith(command_char) and len(first) > len(command_char):
            return first.lower()
        # Spaced: "!" followed by a word token — "! ping"
        if first == command_char and len(parts) > 1 and parts[1].isalpha():
            return (command_char + parts[1]).lower()
        return None

    @property
    def command(self) -> Optional[str]:
        """Convenience property using default '!' — use get_command() in dispatcher."""
        return self.get_command("!")

    @property
    def arg_str(self) -> str:
        """Everything after the command token, handling spaced '! cmd arg' form."""
        parts = self.args
        # Spaced form: ["!", "cmd", ...] where first token is bare command char
        # and second token is the command word (alpha only).
        if (len(parts) >= 2
                and len(parts[0]) == 1          # bare single char
                and not parts[0].isalnum()       # it's a command char
                and parts[1].isalpha()):         # command word (not a number)
            return " ".join(parts[2:])
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def path_len(self) -> Optional[int]:
        return self.raw.get("path_len")

    @property
    def hops(self) -> str:
        pl = self.path_len
        if pl is None:  return "unknown"
        if pl == 255:   return "direct"
        return str(pl)


HandlerFn = Callable[[Message], Awaitable[Optional[str]]]


class CommandEntry(NamedTuple):
    handler:       HandlerFn
    help_text:     str          # short one-line description for summary help
    scope:         str          # registered default: "direct" or "channel"
    priv_floor:    int          # hardcoded minimum — config cannot go below this
    is_admin:      bool
    plugin_name:   str          # used to look up config.plugin(plugin_name)
    cmd_key:       str          # bare command name without "!" for config key lookup
    category:      str          # help grouping — "core" always listed first, rest alphabetical
    usage_text:    str  = ""    # extended usage shown in !help <cmd>
    allow_channel: bool = False # if True, a "direct" default can be widened to "channel" via config
    is_shortcut:   bool = False # if True, hidden from !help index; discoverable via !help <cmd>

# Sentinel used when a plugin doesn't specify a category
_CAT_CORE  = "core"
_CAT_OTHER = "other"


DEFAULT_COMMAND_CHAR = "!"


class Dispatcher:
    def __init__(self, config, db):
        self.config = config
        self.db     = db
        self._commands: Dict[str, CommandEntry] = {}
        self._aliases:  Dict[str, str] = {}   # alias_key → canonical_key
        self._listeners: List[HandlerFn] = []
        self._rehash_callbacks: List = []
        self._reply_queue: asyncio.Queue = asyncio.Queue()
        self._rate_limiter = ChannelRateLimiter(config)
        # Pending confirmation state for disruptive admin commands (!restart, !shutdown).
        # Keyed by sender_id. Entries expire after CONFIRM_TTL_SECONDS.
        # { sender_id: {"action": "restart"|"shutdown", "expires": float} }
        self._pending_confirm: Dict[str, dict] = {}
        # Bot start time — used by !stats for uptime reporting.
        self.started_at: float = time.time()
        # In-memory command usage counters — incremented on every successful
        # dispatch. Reset on restart (intentional — reflects current session).
        # { "!cmd": count }
        self._cmd_usage: Dict[str, int] = {}
        self._register_builtins()

    @property
    def command_char(self) -> str:
        """
        The prefix character that triggers commands. Read live from config so
        !rehash picks up changes. Validated to be exactly one non-alphanumeric,
        non-space character; falls back to DEFAULT_COMMAND_CHAR if invalid.
        """
        val = self.config.get("bot.command_char", DEFAULT_COMMAND_CHAR)
        if isinstance(val, str) and len(val) == 1 and not val.isalnum() and not val.isspace():
            return val
        logger.warning(
            f"Invalid bot.command_char {val!r} — must be a single non-alphanumeric "
            f"character. Using {DEFAULT_COMMAND_CHAR!r}."
        )
        return DEFAULT_COMMAND_CHAR

    # ── Privilege resolution ──────────────────────────────────────────────────

    def resolve_privilege(self, entry: CommandEntry) -> int:
        """
        Resolve effective min_privilege for a command at runtime.

        Order:
          1. Config value: config.plugin(entry.plugin_name)
                               .get("privileges.<cmd_key>")
          2. Hardcoded floor (entry.priv_floor)
        Result is clamped to [priv_floor, 15] so config can restrict
        further but can never grant more access than the floor allows
        lowering to.

        Built-in commands (plugin_name="") skip config lookup and use
        their floor directly.
        """
        if not entry.plugin_name:
            return entry.priv_floor

        cfg_val = self.config.plugin(entry.plugin_name).get(
            f"privileges.{entry.cmd_key}"
        )
        if cfg_val is None:
            return entry.priv_floor

        try:
            configured = int(cfg_val)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid privilege value for {entry.plugin_name}."
                f"privileges.{entry.cmd_key}: {cfg_val!r} — using floor {entry.priv_floor}"
            )
            return entry.priv_floor

        # Clamp: config can raise the floor but never lower it
        return max(entry.priv_floor, min(15, configured))

    def resolve_scope(self, entry: CommandEntry) -> str:
        """
        Resolve the effective scope for a command at runtime.

        Order:
          1. Config value: config.plugin(entry.plugin_name)
                               .get("scopes.<cmd_key>")
             Accepted values: "channel", "direct", "disabled"
          2. Hardcoded default (entry.scope)

        Scope hierarchy (widest → narrowest):
          channel  — responds in DMs and any channel the bot is in
          direct   — DM only
          disabled — silently dropped regardless of source; hidden from !help

        Restriction rules:
          - Any command can be disabled via config regardless of its registered
            default. No opt-in required — operators should always be able to
            turn off a command.
          - "channel" can be tightened to "direct" or "disabled" via config.
          - "direct" can be tightened to "disabled" via config, or widened to
            "channel" only if registered with allow_channel=True.
          - Built-in commands (plugin_name="") always use their registered
            default and cannot be overridden via config.
        """
        if not entry.plugin_name:
            return entry.scope

        cfg_val = self.config.plugin(entry.plugin_name).get(
            f"scopes.{entry.cmd_key}"
        )
        if cfg_val is None:
            return entry.scope

        if cfg_val not in ("direct", "channel", "disabled"):
            logger.warning(
                f"Invalid scope value for {entry.plugin_name}."
                f"scopes.{entry.cmd_key}: {cfg_val!r} — "
                f"must be 'channel', 'direct', or 'disabled'. "
                f"Using default {entry.scope!r}."
            )
            return entry.scope

        # Disabling is always permitted — narrowest possible scope.
        if cfg_val == "disabled":
            return "disabled"

        # Tightening (channel → direct) is always allowed.
        if cfg_val == "direct":
            return "direct"

        # Widening (direct → channel) only if explicitly opted in.
        if cfg_val == "channel" and entry.scope == "direct" and not entry.allow_channel:
            logger.warning(
                f"{entry.plugin_name}.scopes.{entry.cmd_key}=channel ignored — "
                f"this command was not registered with allow_channel=True."
            )
            return "direct"

        return cfg_val


    # ── Built-in commands ─────────────────────────────────────────────────────

    def _register_builtins(self):
        db = self.db

        async def cmd_ping(msg):
            pl = msg.path_len
            if pl is None or pl == 255:
                return "Pong! Path: Direct or Unknown"
            return f"Pong! Path: {pl} hop(s)"

        self.register_command(
            "!ping", cmd_ping,
            help_text="Check connectivity and hop count",
            scope="channel",
            priv_floor=PRIV_DEFAULT,
            category=_CAT_CORE,
        )

        async def cmd_whoami(msg):
            user = await db.get_user(msg.sender_id)
            if not user:
                return "You're not in the registry yet — send any command to register."
            name  = user["display_name"] or msg.sender_id
            priv  = user["privilege"]
            label = _priv_label(priv)
            return (
                f"Name: {name}\n"
                f"ID:   {user['pubkey_prefix']}\n"
                f"Priv: {priv} ({label})"
            )

        self.register_command(
            "!whoami", cmd_whoami,
            help_text="Show your station name, ID, and access level",
            scope="direct",
            priv_floor=PRIV_DEFAULT,
            category=_CAT_CORE,
        )

        async def cmd_rehash(msg):
            who = await db.format_user(msg.sender_id, msg.sender_name)
            logger.warning(f"ADMIN: !rehash by {who}")
            return await self.do_rehash()

        self.register_command(
            "!rehash", cmd_rehash,
            help_text="Reload config without restarting (admin only)",
            scope="direct",
            priv_floor=PRIV_ADMIN,
            is_admin=True,
            category=_CAT_CORE,
        )

        async def cmd_about(msg):
            bot_name      = self.config.get("bot.name", "MeshHall")
            admin_name    = self.config.get("bot.admin_name",  "Name or Callsign not configured")
            admin_contact = self.config.get("bot.admin_contact", "no-email@example.com")
            return (
                f"{bot_name} - a MeshHall bot - meshhall.org\n"
                f"Bot Admin: {admin_name}\n"
                f"Admin Contact: {admin_contact}"
            )

        self.register_command(
            "!about", cmd_about,
            help_text="About this bot and its operator",
            scope="direct",
            priv_floor=PRIV_DEFAULT,
            category=_CAT_CORE,
        )

        # ── !restart / !shutdown — disruptive admin commands ──────────────────
        # Both require a two-step confirmation within CONFIRM_TTL_SECONDS.
        # The actual OS action is injected from meshhall.py via
        # dispatcher.set_system_action_callback() after the event loop starts,
        # so the dispatcher doesn't need to import meshhall directly.

        CONFIRM_TTL = 60  # seconds before pending confirmation expires

        async def _handle_disruptive(msg, action: str):
            """Shared handler for !restart and !shutdown."""
            cc  = self.command_char
            who = await db.format_user(msg.sender_id, msg.sender_name)

            # Check for "confirm" argument
            arg = msg.arg_str.strip().lower()
            if arg == "confirm":
                pending = self._pending_confirm.get(msg.sender_id)
                if not pending or pending["action"] != action:
                    return (
                        f"No pending {cc}{action} request found. "
                        f"Send {cc}{action} first."
                    )
                if time.time() > pending["expires"]:
                    del self._pending_confirm[msg.sender_id]
                    return (
                        f"Confirmation window expired. "
                        f"Send {cc}{action} again to start over."
                    )
                # Confirmed — clear pending, log, send final reply, then act.
                del self._pending_confirm[msg.sender_id]
                logger.warning(f"ADMIN: !{action} confirmed by {who}")
                verb = "Restarting" if action == "restart" else "Shutting down"
                # Enqueue reply directly so it goes out before we act.
                # _drain_before_action() waits for the queue to empty.
                await self._enqueue_reply(msg, f"{verb}...")
                asyncio.create_task(self._drain_and_act(action))
                return None  # reply already enqueued above

            # First invocation — set pending and send warning
            self._pending_confirm[msg.sender_id] = {
                "action":  action,
                "expires": time.time() + CONFIRM_TTL,
            }
            logger.warning(f"ADMIN: !{action} requested by {who} — awaiting confirmation")
            return (
                f"This is disruptive and may require server access to reconcile. "
                f"To proceed, send {cc}{action} confirm\n"
                f"(confirmation expires in {CONFIRM_TTL}s)"
            )

        async def cmd_restart(msg):
            return await _handle_disruptive(msg, "restart")

        async def cmd_shutdown(msg):
            return await _handle_disruptive(msg, "shutdown")

        self.register_command(
            "!restart", cmd_restart,
            help_text="(Admin) Restart the bot process — requires confirmation",
            usage_text="!restart  |  !restart confirm",
            scope="direct",
            priv_floor=PRIV_ADMIN,
            is_admin=True,
            category=_CAT_CORE,
        )

        self.register_command(
            "!shutdown", cmd_shutdown,
            help_text="(Admin) Shut down the bot — requires confirmation",
            usage_text="!shutdown  |  !shutdown confirm",
            scope="direct",
            priv_floor=PRIV_ADMIN,
            is_admin=True,
            category=_CAT_CORE,
        )

    def set_system_action_callback(self, callback):
        """
        Register the callback that performs the actual OS-level restart or shutdown.
        Called from meshhall.py after the event loop is running.
        Signature: async def callback(action: str) where action is "restart" or "shutdown".
        """
        self._system_action_callback = callback

    async def _drain_and_act(self, action: str):
        """
        Wait for the reply queue to drain (so the final reply goes out),
        then invoke the registered system action callback.
        Times out after 10 seconds to avoid hanging indefinitely.
        """
        try:
            await asyncio.wait_for(self._reply_queue.join(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(f"Reply queue did not drain before {action} — proceeding anyway.")

        cb = getattr(self, "_system_action_callback", None)
        if cb:
            await cb(action)
        else:
            logger.error(f"No system action callback registered — cannot {action}.")

    # ── Plugin registration API ───────────────────────────────────────────────

    def register_command(self, command: str, handler: HandlerFn,
                         help_text:     str  = "",
                         scope:         str  = "direct",
                         priv_floor:    int  = PRIV_DEFAULT,
                         is_admin:      bool = False,
                         plugin_name:   str  = "",
                         category:      str  = "",
                         usage_text:    str  = "",
                         allow_channel: bool = False,
                         is_shortcut:   bool = False,
                         # Legacy compat
                         min_privilege: int  = None):
        # Legacy: allow_channel=True at registration time still forces scope
        # to "channel" as a hardcoded default (old behaviour preserved).
        # Going forward plugins set scope="channel" directly; allow_channel is
        # also stored on the entry so resolve_scope can permit config widening.
        if allow_channel:
            scope = "channel"
        if min_privilege is not None and priv_floor == PRIV_DEFAULT:
            priv_floor = min_privilege

        key     = command.lower()
        cmd_key = key.lstrip("!")
        cat     = category or (_CAT_CORE if not plugin_name else _CAT_OTHER)
        self._commands[key] = CommandEntry(
            handler=handler,
            help_text=help_text,
            scope=scope,
            priv_floor=priv_floor,
            is_admin=is_admin,
            plugin_name=plugin_name,
            cmd_key=cmd_key,
            category=cat,
            usage_text=usage_text,
            allow_channel=allow_channel,
            is_shortcut=is_shortcut,
        )
        logger.debug(
            f"Registered: {key} scope={scope} floor={priv_floor} "
            f"allow_channel={allow_channel} cat={cat} plugin={plugin_name or '(builtin)'}"
        )

    def register_admin_command(self, command: str, handler: HandlerFn,
                               help_text:   str = "",
                               scope:       str = "direct",
                               priv_floor:  int = PRIV_ADMIN,
                               plugin_name: str = "",
                               category:    str = "",
                               usage_text:  str = "",
                               allow_channel: bool = False,
                               min_privilege: int  = None):
        self.register_command(
            command, handler,
            help_text=help_text,
            scope=scope,
            priv_floor=priv_floor,
            is_admin=True,
            plugin_name=plugin_name,
            category=category,
            usage_text=usage_text,
            allow_channel=allow_channel,
            min_privilege=min_privilege,
        )

    def register_listener(self, handler: HandlerFn):
        self._listeners.append(handler)

    def register_rehash_callback(self, fn):
        self._rehash_callbacks.append(fn)

    def register_alias(self, alias: str, target: str, *, from_config: bool = False) -> bool:
        """
        Register alias_key → target_key in the command table.

        alias  : the new command name, with or without leading '!' (e.g. "ci" or "!ci")
        target : the existing command to point at (e.g. "checkin" or "!checkin")

        Rules:
          - Target must already be registered.
          - Alias must not collide with a real (non-alias) command.
          - Alias must not point at another alias (no chaining).
          - from_config=True aliases are tracked so they can be cleared on rehash.

        Returns True on success, False on any validation failure (logged as warning).
        """
        alias_key  = "!" + alias.lstrip("!")
        target_key = "!" + target.lstrip("!")

        # Target must exist as a real command
        target_entry = self._commands.get(target_key)
        if not target_entry:
            logger.warning(
                f"Alias '{alias_key}' → '{target_key}': target not found — skipping."
            )
            return False

        # Target must not itself be an alias
        if target_key in self._aliases.values() or target_key in self._aliases:
            # target_key is an alias key — reject
            if target_key in self._aliases:
                logger.warning(
                    f"Alias '{alias_key}' → '{target_key}': target is itself an alias — "
                    "chaining not allowed, skipping."
                )
                return False

        # Alias must not shadow a real command
        if alias_key in self._commands and alias_key not in self._aliases:
            logger.warning(
                f"Alias '{alias_key}' → '{target_key}': name collides with a real "
                "command — skipping."
            )
            return False

        self._aliases[alias_key] = target_key
        # Register in command table pointing at same entry so dispatch resolves normally
        self._commands[alias_key] = target_entry
        source = "config" if from_config else "plugin"
        logger.debug(f"Alias registered ({source}): {alias_key} → {target_key}")
        return True

    def _clear_config_aliases(self):
        """Remove all aliases that were loaded from config (called before rehash reload)."""
        # We track config aliases by storing them in a separate set
        for alias_key in list(getattr(self, "_config_alias_keys", set())):
            self._aliases.pop(alias_key, None)
            # Only remove from _commands if it's still pointing at an alias target
            # (a plugin may have registered a real command with the same name after)
            if alias_key in self._commands and alias_key in self._aliases or \
               alias_key not in self._commands:
                self._commands.pop(alias_key, None)
        self._config_alias_keys: set = set()

    def load_config_aliases(self):
        """
        Read aliases from config.yaml and register them.
        Called after all plugins have loaded, and again on rehash.
        Format in config.yaml:
          aliases:
            absent: regrets
            ci: checkin
        """
        if not hasattr(self, "_config_alias_keys"):
            self._config_alias_keys: set = set()

        aliases = self.config.get("aliases", {}) or {}
        if not isinstance(aliases, dict):
            logger.warning("config.yaml 'aliases' must be a mapping — skipping.")
            return

        registered = 0
        for alias, target in aliases.items():
            alias_key = "!" + str(alias).lstrip("!")
            ok = self.register_alias(alias, str(target), from_config=True)
            if ok:
                self._config_alias_keys.add(alias_key)
                registered += 1

        if registered:
            logger.info(f"Loaded {registered} command alias(es) from config.")

    def log_admin_attempt(self, command: str, msg: Message,
                          granted: bool, reason: str = ""):
        # Sync logging — use sender_name if available; format_user is async
        # and can't be awaited here. Full name enrichment happens at handle() time.
        name   = msg.sender_name or ""
        who    = f"{msg.sender_id} ({name})" if name else msg.sender_id
        source = "DM" if msg.is_dm else f"channel:{msg.channel}"
        verb   = "GRANTED" if granted else "DENIED"
        logger.warning(
            f"ADMIN {verb}: {command} by {who} via {source}"
            + (f" — {reason}" if reason else "")
        )

    # ── Core dispatch ─────────────────────────────────────────────────────────

    async def handle(self, msg: Message):
        # 1. Upsert user — auto-creates at PRIV_DEFAULT, updates name/last_seen.
        #    Skip for sender_id="unknown" (channel messages with no pubkey in
        #    payload) — there is nothing meaningful to store and upsert would
        #    thrash a single shared "unknown" row with every channel sender's name.
        #    Unknown senders get PRIV_DEFAULT; they cannot be muted or privileged.
        if msg.sender_id == "unknown":
            privilege = PRIV_DEFAULT
        else:
            privilege = await self.db.upsert_user(msg.sender_id, msg.sender_name)

        # 2. Muted — silent drop (not applicable to unknown, but kept for clarity)
        if privilege == PRIV_MUTED:
            who = await self.db.format_user(msg.sender_id, msg.sender_name)
            logger.info(f"MUTED: dropped from {who}")
            return

        # 3. Log to DB
        await self.db.log_message(
            msg.ts, msg.channel, msg.sender_id, msg.sender_name, msg.content
        )

        # 4. Listeners
        for listener in self._listeners:
            try:
                await listener(msg)
            except Exception as e:
                logger.error(f"Listener error: {e}")

        # 5. Route command
        #    Detect command using the live command_char. Normalise to "!" prefix
        #    for internal lookup so plugins registered as "!cmd" always resolve,
        #    regardless of what character the operator has configured.
        cc  = self.command_char
        cmd = msg.get_command(cc)

        # Welcome message — sent on first DM or if the user hasn't been
        # welcomed within the configured intro window.
        # Only fires when the message is NOT a command: if someone's first
        # message is "!help", they don't need the intro — they already know
        # how to use the bot.
        if msg.is_dm and msg.sender_id != "unknown" and not cmd:
            await self._maybe_send_welcome(msg)
        if not cmd:
            return
        # Rewrite to canonical "!" prefix for dict lookup
        cmd_key = "!" + cmd[len(cc):]

        # Resolved once per dispatch for all log lines below
        who    = await self.db.format_user(msg.sender_id, msg.sender_name)
        source = "DM" if msg.is_dm else f"channel:{msg.channel}"

        if cmd_key == "!help":
            logger.info(f"CMD: {cmd} by {who} via {source}")
            # In channel: nudge user to DM — we can't send a useful help list
            # into a channel without the user's ID and it would flood the channel.
            if not msg.is_dm:
                bot_name = self.config.get("bot.name", "MeshHall")
                await self._enqueue_reply(
                    msg, f"DM {bot_name} with {cc}help for the full command list."
                )
                return
            # !help <command> — strip optional command_char prefix from arg.
            # Handles: "!help ping", "!help !ping", "/help /ping", "! help ping"
            query = msg.arg_str.strip().lstrip(cc).lstrip("!").strip().lower()
            if query == "admin":
                # Non-admins get unknown-command treatment — no hint it exists.
                if privilege < PRIV_ADMIN:
                    logger.debug(f"!help admin ignored — {who} has priv {privilege}")
                    return
                reply = self._build_admin_help(cc)
            elif query:
                reply = self._build_command_help(query, privilege, cc)
            else:
                reply = self._build_help(privilege, msg.is_dm, cc)
            await self._enqueue_reply(msg, reply)
            return

        entry = self._commands.get(cmd_key)
        if not entry:
            logger.debug(f"Unknown command: {cmd} from {who}")
            return

        # Display command with configured char in log/response messages
        cmd_display = cc + cmd_key.lstrip("!")

        # 6. Scope check — resolved live from config (mirrors privilege resolution)
        effective_scope = self.resolve_scope(entry)
        if effective_scope == "disabled":
            logger.debug(f"{cmd_display} ignored — disabled via config")
            return
        if effective_scope == "direct" and not msg.is_dm:
            logger.debug(f"{cmd_display} ignored — direct-only, received in channel")
            return

        # 7. Privilege check — resolved live from config
        effective_priv = self.resolve_privilege(entry)

        if privilege < effective_priv:
            logger.warning(
                f"PRIV DENIED: {cmd_display} needs {effective_priv}, "
                f"{who} has {privilege} via {source}"
            )
            if msg.is_dm:
                await self._enqueue_reply(
                    msg,
                    f"Access denied. {cmd_display} requires privilege {effective_priv} "
                    f"(you have {privilege})."
                )
            return

        # 8. Rate limit check — channel commands only; DMs are exempt.
        #    Channel bucket exhausted → silent drop (logged in rate limiter).
        #    Sender bucket exhausted  → warn once, then silent drop.
        if not msg.is_dm:
            rl = self._rate_limiter.check(msg)
            if rl.is_sender_warn:
                secs = int(rl.retry_seconds) + 1
                await self._enqueue_reply(
                    msg,
                    f"Slow down — rate limit reached. Try again in ~{secs}s."
                )
                return
            if rl.is_channel_limit or rl.is_sender_silent:
                return

        # 9. Execute
        if entry.is_admin:
            logger.warning(f"ADMIN CMD: {cmd_display} by {who} via {source}")
        else:
            logger.info(
                f"CMD: {cmd_display} by {who} via {source} priv={privilege}/{effective_priv}"
            )

        # Track usage for !stats — incremented before handler so even commands
        # that return errors are counted (reflects usage intent, not success).
        self._cmd_usage[cmd_key] = self._cmd_usage.get(cmd_key, 0) + 1

        try:
            reply = await entry.handler(msg)
            if reply:
                await self._enqueue_reply(msg, reply)
        except Exception as e:
            logger.error(f"Handler error for {cmd_display}: {e}", exc_info=True)
            await self._enqueue_reply(msg, "Internal error processing command.")

    async def _maybe_send_welcome(self, msg: Message):
        """
        Send a welcome DM if this is the user's first contact or if the
        intro_window has elapsed since last welcome.
        Configured via bot.intro_window_minutes (default 60, 0 = first-time only).
        """
        try:
            user = await self.db.get_user(msg.sender_id)
            if user is None:
                return  # upsert_user just ran; race is benign — next message will welcome

            intro_window = int(self.config.get("bot.intro_window_minutes", 60)) * 60
            welcomed_ts  = user["welcomed_ts"]
            now          = int(time.time())

            if welcomed_ts is not None:
                if intro_window == 0:
                    return  # first-time only, already welcomed
                if now - welcomed_ts < intro_window:
                    return  # within window — don't repeat

            bot_name = self.config.get("bot.name", "MeshHall")
            cc       = self.command_char
            welcome  = f"Hi, I'm {bot_name} - a MeshHall bot. Use {cc}help for assistance."
            await self.db.set_welcomed(msg.sender_id)
            await self._enqueue_reply(msg, welcome)
        except Exception as e:
            logger.warning(f"Welcome message error for {msg.sender_id}: {e}")

    async def _enqueue_reply(self, msg: Message, text: str):
        # For channel messages, prefix the reply with @[sender_name] so the
        # recipient knows the response is directed at them.
        if msg.channel and msg.sender_name:
            text = f"@[{msg.sender_name}]\n{text}"

        chunks = chunk_text(text, MAX_CHUNK)
        # Carry channel_idx from raw payload so the connection layer can pass
        # the integer slot number to send_channel_msg (preferred over label).
        channel_idx = msg.raw.get("channel_idx", msg.raw.get("channel")) if msg.raw else None
        for i, chunk in enumerate(chunks):
            await self._reply_queue.put({
                "target_id":   msg.sender_id,
                "channel":     msg.channel,
                "channel_idx": channel_idx,
                "text":        chunk,
                "part":        i + 1,
                "total":       len(chunks),
            })

    def _build_help(self, privilege: int = PRIV_DEFAULT,
                    is_dm: bool = True,
                    command_char: str = "!") -> str:
        """
        Simplified summary help — one line per command, no categories.
        Admin commands and aliases are excluded from the index entirely.
        Admins get a note directing them to !help admin for their commands.
        Disabled commands are hidden regardless of privilege.
        """
        lines = [f"Use {command_char}help <command> for details\n"]

        if privilege >= PRIV_ADMIN:
            lines.append(f"Use {command_char}help admin for admin commands.\n")

        for stored_cmd, entry in sorted(self._commands.items()):
            if not entry.help_text:
                continue
            if entry.is_admin:
                continue
            if stored_cmd in self._aliases:
                continue
            if entry.is_shortcut:
                continue
            effective_scope = self.resolve_scope(entry)
            if effective_scope == "disabled":
                continue
            if privilege < self.resolve_privilege(entry):
                continue
            if not is_dm and effective_scope == "direct":
                continue
            display_cmd = command_char + stored_cmd.lstrip("!")
            short = entry.help_text.split("Usage:")[0].rstrip(". ")
            lines.append(f"{display_cmd}: {short}")

        if len(lines) == 1:
            return "MeshHall — no commands available at your privilege level."

        return "\n".join(lines)

    def _build_admin_help(self, command_char: str = "!") -> str:
        """
        Admin-only command index. Only shown to users with PRIV_ADMIN.
        Non-admins get unknown-command treatment at the call site.
        """
        lines = [f"Admin commands — use {command_char}help <command> for details\n"]
        for stored_cmd, entry in sorted(self._commands.items()):
            if not entry.is_admin:
                continue
            if not entry.help_text:
                continue
            if stored_cmd in self._aliases:
                continue
            if entry.is_shortcut:
                continue
            effective_scope = self.resolve_scope(entry)
            if effective_scope == "disabled":
                continue
            display_cmd = command_char + stored_cmd.lstrip("!")
            short = entry.help_text.split("Usage:")[0].rstrip(". ")
            lines.append(f"{display_cmd}: {short}")

        if len(lines) == 1:
            return "No admin commands registered."
        return "\n".join(lines)

    def _build_command_help(self, query: str, privilege: int,
                             command_char: str = "!") -> str:
        """
        Context-sensitive help for a single command or alias.
        Returns full description + usage + scope info.
        Disabled commands are treated as unknown — silent from the user's view.
        """
        stored_cmd = "!" + query.lstrip("!")

        # Resolve alias — one level only, no chaining
        is_alias   = stored_cmd in self._aliases
        target_key = self._aliases.get(stored_cmd, stored_cmd)
        entry      = self._commands.get(target_key)

        if not entry:
            return f"Unknown command: {command_char}{query}"

        # Treat disabled the same as unknown
        if self.resolve_scope(entry) == "disabled":
            return f"Unknown command: {command_char}{query}"

        eff_priv = self.resolve_privilege(entry)
        if privilege < eff_priv:
            return f"Access denied. {command_char}{query} requires privilege {eff_priv}."

        display_cmd = command_char + target_key.lstrip("!")
        lines = [f"{display_cmd}: {entry.help_text}"]
        if is_alias:
            alias_display = command_char + stored_cmd.lstrip("!")
            lines.append(f"(Alias: {alias_display} → {display_cmd})")
        elif entry.is_shortcut:
            lines.append(f"(Shortcut — see {display_cmd} for full usage)")
        if entry.usage_text:
            lines.append(f"Usage: {entry.usage_text}")
        eff_scope  = self.resolve_scope(entry)
        scope_note = "DM or channel" if eff_scope == "channel" else "DM only"
        lines.append(f"Scope: {scope_note} | Priv: {eff_priv}")
        return "\n".join(lines)

    async def do_rehash(self) -> str:
        logger.info("Rehash triggered.")
        errors = self.config.reload()

        results = ["Config reloaded."]
        if errors:
            results.append(f"{len(errors)} file(s) had errors (old values kept):")
            for path, err in errors:
                results.append(f"  {path}: {err}")
        else:
            results.append("No errors.")

        # Re-promote admins in case bot.admins changed
        admin_ids = self.config.get("bot.admins", [])
        for admin_id in admin_ids:
            await self.db.upsert_user(admin_id)
            await self.db.set_privilege(admin_id, PRIV_ADMIN)
        if admin_ids:
            results.append(f"Admin privileges refreshed for {len(admin_ids)} user(s).")

        # Reload config-defined aliases (clear old ones first)
        self._clear_config_aliases()
        self.load_config_aliases()

        for cb in self._rehash_callbacks:
            try:
                result = await cb()
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Rehash callback error: {e}")

        summary = "\n".join(results)
        logger.info(f"Rehash complete: {summary}")
        return summary

    @property
    def reply_queue(self) -> asyncio.Queue:
        return self._reply_queue

    @property
    def cmd_usage(self) -> Dict[str, int]:
        """Read-only view of per-command dispatch counts since last restart."""
        return dict(self._cmd_usage)

    async def enqueue_dm(self, target_id: str, text: str):
        """
        Send a DM to a specific node by pubkey_prefix, independent of any
        incoming Message context. Runs text through the standard chunker so
        the 156-byte firmware limit is respected automatically.

        Use this from plugins that need to push unsolicited DMs (e.g. MOTD
        delivery, alert notifications) rather than writing directly to
        reply_queue, which bypasses chunking.
        """
        chunks = chunk_text(text, MAX_CHUNK)
        for i, chunk in enumerate(chunks):
            await self._reply_queue.put({
                "target_id":   target_id,
                "channel":     None,
                "channel_idx": None,
                "text":        chunk,
                "part":        i + 1,
                "total":       len(chunks),
            })


def _priv_label(priv: int) -> str:
    if priv == PRIV_MUTED:   return "muted"
    if priv == PRIV_DEFAULT: return "default"
    if priv == PRIV_ADMIN:   return "admin"
    return f"level-{priv}"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _find_last(text: str, char: str, max_bytes: int) -> int:
    """Last index of char in text where text[:index] fits within max_bytes UTF-8 bytes."""
    best = -1
    b    = 0
    for i, c in enumerate(text):
        if b >= max_bytes:
            break
        if c == char:
            best = i
        b += _byte_len(c)
    return best


def chunk_text(text: str, max_bytes: int = MAX_CHUNK) -> List[str]:
    """
    Split text into chunks whose UTF-8 byte length — including the [part/total]
    prefix added by _send_reply — never exceeds max_bytes.

    The firmware limit is 156 bytes, not characters, so emoji and other
    multibyte codepoints are counted correctly.

    Split preference within the byte budget: newline > space > hard cut.
    """
    if _byte_len(text) <= max_bytes:
        return [text]

    PREFIX_OVERHEAD = 9   # "[99/99]\n" worst case (newline separator after prefix)
    chunk_budget    = max_bytes - PREFIX_OVERHEAD

    chunks: List[str] = []
    while text:
        if _byte_len(text) <= chunk_budget:
            chunks.append(text)
            break

        split = _find_last(text, "\n", chunk_budget)
        if split != -1:
            chunks.append(text[:split])
            text = text[split + 1:]
            continue

        split = _find_last(text, " ", chunk_budget)
        if split != -1:
            chunks.append(text[:split])
            text = text[split + 1:]
            continue

        # Hard cut — find max chars fitting in budget
        i = b = 0
        while i < len(text):
            cb = _byte_len(text[i])
            if b + cb > chunk_budget:
                break
            b += cb
            i += 1
        chunks.append(text[:i])
        text = text[i:]

    return [c for c in chunks if c]
