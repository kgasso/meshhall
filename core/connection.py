"""
ConnectionManager -- wraps the meshcore library using its actual API.

The meshcore library uses factory methods (create_serial, create_tcp) and
a subscription/event model rather than a constructor + async iterator.

Real API (meshcore 2.x):
    mc = await MeshCore.create_serial("/dev/ttyACM0", 115200)
    mc = await MeshCore.create_tcp("host", port)
    mc.subscribe(EventType.CONTACT_MSG_RECV, handler)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, handler)
    await mc.start_auto_message_fetching()
    await mc.commands.send_msg(contact, text)
    await mc.disconnect()

ACK behaviour (confirmed via diagnostics):
    The MeshCore client marks messages as delivered when the node receives
    the RF packet -- no application-level ACK is needed or possible in 2.x.
    Duplicates are caused by the node replaying buffered messages on bot
    reconnect. Dedup is persisted to SQLite so restarts don't re-execute
    buffered commands.

Channel architecture:
    Channels are enumerated from the radio at startup and rehash via
    CMD_GET_CHANNEL (slot 0-7). The channel name, respond flag, and a
    safety disabled_at timestamp are stored in the _channels DB table.
    No channel configuration lives in config.yaml -- the radio is the
    source of truth for what channels exist and what they are named.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import asyncio
import logging
import time
from typing import Optional

from core.dispatcher import Dispatcher, Message

logger = logging.getLogger(__name__)

# -- DB schemas registered at startup ------------------------------------------

# Dedup table -- survives bot restarts so buffered message replay on reconnect
# doesn't re-execute commands.
DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS _dedup (
    key         TEXT PRIMARY KEY,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedup_ts ON _dedup(ts);
"""

# Channel table -- one row per non-empty radio slot, keyed by channel_idx.
# respond=1 means the bot will reply into the channel; 0 = listen-only.
# disabled_at is set (epoch ts) when the bot auto-disables a slot because
# the channel name changed since last enumeration -- safety guard against
# inadvertently replying to a different channel after a radio reconfiguration.
# last_seen is the epoch ts of the most recent successful enumeration.
CHANNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS _channels (
    channel_idx  INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    respond      INTEGER NOT NULL DEFAULT 0,
    last_seen    INTEGER NOT NULL DEFAULT 0,
    disabled_at  INTEGER
);
"""

# Maximum channel slots to probe (MeshCore firmware cap is 8, slots 0-7).
def _strip_channel_name_prefix(content: str, sender_name: str = None) -> str:
    """
    Strip the "DisplayName: " prefix that MeshCore firmware prepends to all
    channel message text before delivering it to the library.

    Strategy:
      1. If sender_name is known, check for an exact "sender_name: " prefix
         (case-insensitive). Strip it if found.
      2. Otherwise, strip any "words: " prefix -- defined as one or more
         non-colon tokens (which may include emoji/punctuation) followed by
         ": " -- as long as what remains is non-empty.

    We are deliberately permissive: if the content doesn't look like it has
    a name prefix, it's returned unchanged. A false-strip would be worse than
    leaving the prefix in, since the user might legitimately start a message
    with a word and colon.
    """
    if not content:
        return content

    # Strategy 1: known sender name
    if sender_name:
        prefix = sender_name + ": "
        if content.startswith(prefix):
            return content[len(prefix):]
        # Case-insensitive fallback
        if content.lower().startswith(prefix.lower()):
            return content[len(prefix):]

    # Strategy 2: pattern match -- "anything up to first ': '" where the
    # remainder starts with a command character or looks like a command.
    # Only strip if the part after ": " is non-empty.
    sep = ": "
    idx = content.find(sep)
    if idx > 0:
        remainder = content[idx + len(sep):]
        if remainder:
            return remainder

    return content


MAX_CHANNEL_SLOTS = 8

# Maximum length enforced on all inbound display names.
# MeshCore firmware typically caps node names at 20-30 chars;
# 48 gives headroom for future changes without allowing abuse.
_NAME_MAX_LEN = 48

def _sanitise_name(name) -> str:
    """
    Sanitise a user-supplied display name at the point of ingestion.

    Applies two defences:
      1. Strip control characters -- newlines, carriage returns, null bytes,
         and other non-printable ASCII that could break output formatting
         or inject fake message lines into bot replies.
      2. Truncate to _NAME_MAX_LEN characters -- prevents oversized names
         from bloating DB rows or wrapping output unexpectedly.

    Called on every inbound sender_name before it reaches the Message
    object, the DB, or any output formatter. Returns None if the input
    is None or becomes empty after stripping.
    """
    if not name:
        return None
    # Remove control characters (ord < 32) and DEL (127), keep printable UTF-8
    cleaned = "".join(ch for ch in str(name) if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:_NAME_MAX_LEN]


class ConnectionManager:
    def __init__(self, config, dispatcher: Dispatcher, db=None):
        self.config = config
        self.dispatcher = dispatcher
        self._db = db
        self._running = False
        self._mc = None
        self._contacts  = {}
        self._adv_method = None   # set after connection probe in run()

        # In-memory channel state rebuilt from DB after every enumeration.
        # Keys are channel_idx integers; values are dicts with name/respond/disabled_at.
        self._channels: dict = {}

        # Deduplication -- in-memory cache backed by SQLite for restart persistence.
        # Key: "{sender_id}:{sender_timestamp}"
        # Value: time.time() when first seen
        self._dedup_mem: dict = {}
        self._dedup_window: float = config.get("connection.dedup_window_seconds", 120.0)

        # Lock to prevent concurrent get_contacts() calls (e.g. periodic refresh
        # racing with an ADV event handler).
        self._contacts_lock = asyncio.Lock()

        if db:
            db.register_schema(DEDUP_SCHEMA)
            db.register_schema(CHANNEL_SCHEMA)

    async def run(self):
        """
        Run the bot until stop() is called.

        Uses asyncio.gather() with two long-running coroutines. stop() cancels
        the gather task, which causes run() to return normally. main() then
        returns normally, and asyncio.run() exits with code 0 -- no sys.exit(),
        no event loop stop(), no RuntimeError.
        """
        self._running = True
        self._run_task = asyncio.current_task()
        try:
            await asyncio.gather(
                self._connect_loop(),
                self._reply_drain_loop(),
            )
        except asyncio.CancelledError:
            pass  # clean shutdown via stop() -- not an error

    async def stop(self):
        """
        Signal the bot to stop and wait for cleanup.

        Sets _running=False (stops retry loops), disconnects the radio,
        then cancels the gather task so run() returns. Awaited by shutdown
        coroutines -- after this returns, main() can return and asyncio.run()
        exits with code 0.
        """
        self._running = False
        if self._mc:
            try:
                await self._mc.stop_auto_message_fetching()
                await self._mc.disconnect()
            except Exception:
                pass
        # Cancel the run() task so asyncio.gather() unblocks and run() returns
        run_task = getattr(self, "_run_task", None)
        if run_task and not run_task.done():
            run_task.cancel()
            try:
                await asyncio.shield(asyncio.sleep(0))  # yield to let cancel propagate
            except asyncio.CancelledError:
                pass

    # -- Connection with auto-reconnect ----------------------------------------

    async def _connect_loop(self):
        retry_delay = 5
        while self._running:
            try:
                await self._connect()
                retry_delay = 5
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
                self._mc = None
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def _connect(self):
        try:
            from meshcore import MeshCore, EventType
        except ImportError:
            raise RuntimeError(
                "meshcore library not installed. Run: "
                "/opt/meshhall/venv/bin/pip install meshcore"
            )

        conn_type = self.config.get("connection.type", "serial")
        logger.info(f"Connecting via {conn_type}...")

        if conn_type == "serial":
            port = self.config.get("connection.serial_port", "/dev/ttyACM0")
            baud = self.config.get("connection.baud_rate", 115200)
            self._mc = await MeshCore.create_serial(port, baud)
        elif conn_type == "tcp":
            host = self.config.get("connection.tcp_host", "localhost")
            port = self.config.get("connection.tcp_port", 5000)
            self._mc = await MeshCore.create_tcp(host, port)
        else:
            raise ValueError(f"Unknown connection type: {conn_type}")

        logger.info("Connected to MeshCore node.")

        # Populate contacts cache and seed user registry -- verbose on startup
        await self._refresh_contacts(verbose=True)

        # Subscribe to direct messages (contact -> bot)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

        # Subscribe to channel messages
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)

        # Subscribe to advertisements and new contacts.
        # Probe defensively -- event types vary across 2.x versions.
        for evt_name in ("ADVERTISEMENT", "NEW_CONTACT", "CONTACT_UPDATE"):
            evt = getattr(EventType, evt_name, None)
            if evt is not None:
                self._mc.subscribe(evt, self._on_advertisement)
                logger.info(f"Subscribed to EventType.{evt_name} for display name tracking")
            else:
                logger.info(
                    f"EventType.{evt_name} not available in this meshcore version -- "
                    "display names will be populated from contacts cache and inbound messages only"
                )

        # Log all available EventTypes at startup for diagnostics
        all_events = [e for e in dir(EventType) if not e.startswith("_")]
        logger.info(f"Available EventTypes: {all_events}")

        # Probe for self-advertisement capability
        adv_method      = None
        adv_method_name = None
        _adv_candidates = ("send_advert", "send_advertise", "advertise", "send_adv", "send_advertisement")
        _available_cmds = [m for m in dir(self._mc.commands) if not m.startswith("_")]
        logger.info(f"Bot advertisement probe -- available commands: {_available_cmds}")
        for method_name in _adv_candidates:
            if hasattr(self._mc.commands, method_name):
                _raw            = getattr(self._mc.commands, method_name)
                # send_advert requires flood=True to propagate beyond direct neighbours
                if method_name == "send_advert":
                    adv_method = lambda: _raw(flood=True)
                else:
                    adv_method = _raw
                adv_method_name  = method_name
                self._adv_method = adv_method
                logger.info(f"Bot advertisement: will use commands.{method_name}()"
                            + (" with flood=True" if method_name == "send_advert" else ""))
                break
        if adv_method is None:
            logger.warning(
                f"Bot advertisement: none of {_adv_candidates} found -- "
                "bot will not self-advertise. Check meshcore version."
            )

        # Start background message polling
        await self._mc.start_auto_message_fetching()
        logger.info("Subscribed to messages. Bot is live.")

        # Enumerate channel slots from the radio and reconcile with DB
        await self.enumerate_channels()

        # Register dispatcher providers now that the connection is live.
        # Co-locating registration here means no wiring needed in meshhall.py --
        # any plugin or the web API can call dispatcher.get_radio_contact_count()
        # or dispatcher.get_node_info() without knowing about ConnectionManager.
        self.dispatcher.register_contact_count_provider(self.get_radio_contact_count)
        self.dispatcher.register_node_info_provider(self.get_node_info)
        self.dispatcher.register_channel_by_name_provider(self._get_channel_by_name)
        self.dispatcher.register_contacts_snapshot_provider(self._get_contacts_snapshot)
        logger.info("Dispatcher providers registered: contact_count, node_info, channel_by_name, contacts_snapshot")

        # Backfill contacts on startup -- populates full_public_key, contact_type,
        # and display_name for all nodes already in the radio contact list before
        # waiting for them to advertise.
        logger.info("Running startup contacts backfill...")
        await self._refresh_contacts(verbose=True)

        # Keep the connection alive until disconnected
        dedup_prune_interval   = 3600
        last_dedup_prune       = time.time()
        adv_interval           = self.config.get("bot.advertise_interval", 0)
        last_adv               = 0.0   # send first advert immediately if enabled
        contact_prune_interval = int(self.config.get("bot.contact_prune_interval", 900))
        last_contact_prune     = time.time() - contact_prune_interval + 60  # first run ~60s after startup

        while self._running:
            await asyncio.sleep(30)

            # Periodic contacts refresh (quiet -- only logs changes)
            try:
                await self._refresh_contacts()
            except Exception as e:
                logger.warning(f"Contact refresh error: {e}")

            # Self-advertisement
            if adv_method and adv_interval > 0:
                if time.time() - last_adv >= adv_interval:
                    try:
                        await adv_method()
                        logger.info("Bot advertisement sent.")
                        last_adv = time.time()
                    except Exception as e:
                        logger.warning(f"Bot advertisement failed: {e}")

            # Contact list prune
            if contact_prune_interval > 0:
                if time.time() - last_contact_prune >= contact_prune_interval:
                    try:
                        await self._prune_contacts()
                    except Exception as e:
                        logger.warning(f"Contact prune error: {e}")
                    last_contact_prune = time.time()

            # Dedup DB prune
            if time.time() - last_dedup_prune > dedup_prune_interval:
                await self._prune_dedup_db()
                last_dedup_prune = time.time()

    # -- Channel enumeration ---------------------------------------------------

    async def enumerate_channels(self) -> str:
        """
        Query the radio for all channel slots (0 to MAX_CHANNEL_SLOTS-1),
        skip empty slots, and reconcile results with the _channels DB table.

        Reconciliation rules:
          - New slot (not in DB): INSERT with respond=0, log at INFO.
          - Known slot, same name: UPDATE last_seen only.
          - Known slot, name changed: set respond=0, set disabled_at=now,
            log a WARNING. Operator must re-enable via !channel <idx> on.
          - Known slot, now empty: leave DB row untouched (radio may be
            temporarily misconfigured); log at INFO.

        Radio unavailability (no get_channel API, timeout, error): keeps
        existing DB state -- no slots are disabled purely due to read failure.

        Returns a human-readable summary string (used by !channel sync and
        in startup logs).
        """
        if not self._mc:
            return "Not connected to radio -- channel enumeration skipped."

        # Probe for CMD_GET_CHANNEL wrapper in the library
        get_ch_fn = None
        for method_name in ("get_channel", "get_channel_info", "read_channel"):
            if hasattr(self._mc.commands, method_name):
                get_ch_fn = getattr(self._mc.commands, method_name)
                logger.info(f"Channel enumeration: using commands.{method_name}()")
                break

        if get_ch_fn is None:
            logger.warning(
                "Channel enumeration: no get_channel API found in this meshcore "
                "version. Channel management commands will reflect DB state only. "
                "Upgrade meshcore or configure channels directly via !channel."
            )
            await self._reload_channel_cache()
            return "No get_channel API available -- using cached DB state."

        now = int(time.time())
        new_slots = []
        changed_slots = []
        seen_slots = []
        new_slots_idxs = []
        changed_idxs = []

        for idx in range(MAX_CHANNEL_SLOTS):
            try:
                result = await get_ch_fn(idx)
            except Exception as e:
                logger.warning(f"get_channel({idx}) failed: {e}")
                continue

            # Extract name from result -- handle both dict payload and attribute access
            if hasattr(result, "payload") and isinstance(result.payload, dict):
                raw_name = result.payload.get("name", "") or result.payload.get("channel_name", "")
            elif hasattr(result, "payload") and isinstance(result.payload, str):
                raw_name = result.payload
            else:
                raw_name = str(getattr(result, "name", "") or "")

            # Log raw payload at DEBUG so operators can see exact field names
            logger.debug(f"get_channel({idx}) raw payload: {getattr(result, 'payload', result)!r}")

            name = raw_name.strip().rstrip("\x00")  # strip nulls from fixed-width C strings
            if not name:
                logger.debug(f"Channel slot {idx}: empty -- skipping")
                continue

            # Reconcile with DB
            existing = await self._db.fetchone(
                "SELECT name, respond, disabled_at FROM _channels WHERE channel_idx=?", (idx,)
            )

            if existing is None:
                # New slot -- add with respond=0
                await self._db.execute(
                    "INSERT INTO _channels (channel_idx, name, respond, last_seen, disabled_at) "
                    "VALUES (?, ?, 0, ?, NULL)",
                    (idx, name, now),
                )
                await self._db.commit()
                logger.info(f"Channel slot {idx}: new -- name={name!r} respond=off")
                new_slots.append(f"[{idx}] {name} (new, respond=off)")
                new_slots_idxs.append(idx)

            elif existing["name"] != name:
                # Name changed -- auto-disable channel and any announcements targeting it
                old_ch_name = existing["name"]
                await self._db.execute(
                    "UPDATE _channels SET name=?, respond=0, last_seen=?, disabled_at=? "
                    "WHERE channel_idx=?",
                    (name, now, now, idx),
                )
                await self._db.commit()
                logger.warning(
                    f"Channel slot {idx}: name changed {old_ch_name!r} -> {name!r}. "
                    f"Respond DISABLED (was {'on' if existing['respond'] else 'off'}). "
                    f"Use '!channel {idx} on' to re-enable after verifying."
                )
                disabled_ann = await self._disable_announcements_for_channel(old_ch_name)
                if disabled_ann:
                    logger.warning(
                        f"Announcements disabled due to channel rename "
                        f"{old_ch_name!r}->{name!r}: {', '.join(disabled_ann)}"
                    )
                changed_slots.append(
                    f"[{idx}] {existing['name']!r}->{name!r} DISABLED"
                )
                changed_idxs.append(idx)

            else:
                # Same name -- just touch last_seen
                await self._db.execute(
                    "UPDATE _channels SET last_seen=? WHERE channel_idx=?",
                    (now, idx),
                )
                await self._db.commit()
                seen_slots.append(idx)  # already tracked for gone-slot sweep

        # Sweep DB slots that were not seen in this radio scan.
        # A missing slot may mean the radio was reconfigured -- disable any
        # announcements that targeted the now-absent channel name.
        all_db_slots = await self._db.fetchall(
            "SELECT channel_idx, name FROM _channels"
        )
        scanned_idxs = set(new_slots_idxs) | set(changed_idxs) | set(seen_slots)
        gone_ann_notes = []
        for db_row in all_db_slots:
            if db_row["channel_idx"] not in scanned_idxs:
                gone_name = db_row["name"]
                disabled_ann = await self._disable_announcements_for_channel(gone_name)
                if disabled_ann:
                    gone_ann_notes.append(
                        f"slot {db_row['channel_idx']} ({gone_name!r}): "
                        + ", ".join(disabled_ann)
                    )
                    logger.warning(
                        f"Channel slot {db_row['channel_idx']} ({gone_name!r}) not seen "
                        f"in radio scan -- announcements disabled: {', '.join(disabled_ann)}"
                    )

        # Rebuild in-memory cache from DB
        await self._reload_channel_cache()

        # Build summary
        parts = []
        if new_slots:
            parts.append(f"{len(new_slots)} new: {', '.join(new_slots)}")
        if changed_slots:
            parts.append(f"{len(changed_slots)} name-changed (disabled): {', '.join(changed_slots)}")
        if seen_slots:
            parts.append(f"{len(seen_slots)} unchanged: slots {seen_slots}")
        if gone_ann_notes:
            parts.append(f"announcements disabled (gone slots): {'; '.join(gone_ann_notes)}")
        if not parts:
            parts.append("No channel slots found on radio.")

        summary = "Channel sync: " + "; ".join(parts)
        logger.info(summary)
        return summary

    async def _disable_announcements_for_channel(self, channel_name: str) -> list:
        """
        Set active=0 on all announcements targeting channel_name.
        Returns a list of disabled slug names (may be empty).
        Safe to call even if the announcements table does not exist yet
        (e.g. announce plugin not loaded).
        """
        try:
            rows = await self._db.fetchall(
                "SELECT slug FROM announcements WHERE channel=? AND active=1",
                (channel_name,),
            )
            if not rows:
                return []
            slugs = [r["slug"] for r in rows]
            now = int(time.time())
            await self._db.execute(
                "UPDATE announcements SET active=0 WHERE channel=? AND active=1",
                (channel_name,),
            )
            await self._db.commit()
            return slugs
        except Exception as e:
            # announcements table may not exist if plugin is disabled -- not an error
            logger.debug(f"_disable_announcements_for_channel({channel_name!r}): {e}")
            return []

    async def _reload_channel_cache(self):
        """Rebuild self._channels from the DB. Called after any enumeration or respond change."""
        rows = await self._db.fetchall("SELECT * FROM _channels")
        self._channels = {
            row["channel_idx"]: {
                "name":        row["name"],
                "respond":     bool(row["respond"]),
                "disabled_at": row["disabled_at"],
            }
            for row in rows
        }

    def _channel_is_known(self, channel_idx: int) -> bool:
        """Return True if this slot is in our channel table (regardless of respond flag)."""
        return channel_idx in self._channels

    def _channel_should_respond(self, channel_idx: int) -> bool:
        """Return True if the bot should reply into this channel slot."""
        ch = self._channels.get(channel_idx)
        return bool(ch and ch["respond"] and ch["disabled_at"] is None)

    # -- Contacts --------------------------------------------------------------

    async def _refresh_contacts(self, verbose: bool = False):
        """
        Fetch contacts from node and upsert into user registry.

        Key facts confirmed from live logs:
          - contacts payload is a dict keyed by full 64-char public_key
          - each value is a dict with: public_key, adv_name, last_advert, etc.
          - pubkey_prefix (12-char) = first 12 chars of public_key
          - ADVERTISEMENT events only carry public_key -- no name -- so we
            refresh contacts after each advertisement to pick up adv_name

        A lock prevents concurrent get_contacts() calls from racing when an ADV
        event fires during a scheduled refresh.
        """
        if self._contacts_lock.locked():
            logger.debug("_refresh_contacts: skipping -- refresh already in progress")
            return

        async with self._contacts_lock:
            try:
                from meshcore import EventType
                result = await self._mc.commands.get_contacts()

                if result.type == EventType.ERROR:
                    logger.warning(f"get_contacts() returned ERROR: {result.payload}")
                    return

                payload = result.payload
                if not payload or not isinstance(payload, dict):
                    if verbose:
                        logger.debug(f"get_contacts() empty or unexpected: {type(payload).__name__}")
                    return

                seeded = 0
                updated = 0
                for full_key, contact in payload.items():
                    if not isinstance(contact, dict):
                        continue

                    pubkey_prefix = str(full_key)[:12]

                    # Skip malformed or missing keys -- don't create/update a shared
                    # "unknown" row that would thrash with every contact refresh.
                    if not pubkey_prefix or pubkey_prefix == "unknown":
                        logger.debug(f"_refresh_contacts: skipping contact with bad key {full_key!r}")
                        continue

                    name = _sanitise_name(contact.get("adv_name") or contact.get("name")
                            or contact.get("display_name"))

                    self._contacts[str(full_key)] = contact
                    self._contacts[pubkey_prefix] = contact  # alias for fast lookup

                    if verbose:
                        logger.debug(
                            f"Contact: prefix={pubkey_prefix} full={str(full_key)[:16]}... "
                            f"name={name!r} last_advert={contact.get('last_advert')}"
                        )

                    try:
                        contact_type = contact.get("type")
                        old_name     = None
                        is_new = await self._db.upsert_contact(
                            pubkey_prefix, str(full_key), name, contact_type,
                            update_advert_ts=False,
                            _return_old_name=True,
                        )
                        if isinstance(is_new, tuple):
                            is_new, old_name = is_new
                        if is_new:
                            seeded += 1
                            logger.info(
                                f"New contact: {pubkey_prefix} ({name!r})"
                                + (f" type={contact_type}" if contact_type is not None else "")
                            )
                        elif name and old_name is not None and name != old_name:
                            updated += 1
                    except Exception as e:
                        logger.warning(f"upsert_contact failed for {pubkey_prefix}: {e}")

                if seeded or updated or verbose:
                    logger.info(
                        f"Contacts refresh: {len(payload)} contacts, "
                        f"{seeded} new, {updated} name updates"
                    )

            except Exception as e:
                logger.warning(f"_refresh_contacts error: {e}", exc_info=True)

    async def _on_advertisement(self, event):
        """
        Handle ADVERTISEMENT and NEW_CONTACT events.

        Refreshes the contacts cache to pick up adv_name and contact type,
        then upserts the user record with full_public_key and last_advert_ts.

        Contact list management (auto-add) is handled by the radio firmware --
        enable "Auto Add" under Contact Settings on the node. The bot's prune
        task keeps the list under the radio's contact limit.
        """
        try:
            payload = event.payload
            if not payload:
                return

            if isinstance(payload, dict):
                full_key = payload.get("public_key") or payload.get("pubkey_prefix") or ""
            else:
                full_key = str(getattr(payload, "public_key", "") or
                               getattr(payload, "pubkey_prefix", ""))

            full_key      = str(full_key)
            pubkey_prefix = full_key[:12] if full_key else ""

            logger.info(
                f"ADV received: prefix={pubkey_prefix} "
                f"full={full_key[:16]}{'...' if len(full_key) > 16 else ''}"
            )

            # Refresh contacts to get adv_name and contact metadata
            await self._refresh_contacts()

            if not pubkey_prefix:
                return

            # Get name, type, and hop count from refreshed contacts cache + ADV payload
            contact      = self._contacts.get(pubkey_prefix, {})
            name         = _sanitise_name(
                contact.get("adv_name") or contact.get("name") or contact.get("display_name")
            )
            contact_type = contact.get("type")

            # path_len may arrive in the ADV payload itself or in the cached contact dict
            raw_hops = None
            if isinstance(payload, dict):
                raw_hops = payload.get("path_len")
            if raw_hops is None:
                raw_hops = contact.get("path_len")
            last_hops = int(raw_hops) if raw_hops is not None else None

            logger.debug(
                f"ADV contact detail: prefix={pubkey_prefix} "
                f"name={name!r} type={contact_type!r} hops={last_hops!r} "
                f"keys={list(contact.keys()) if contact else '(not in cache)'}"
            )

            # Upsert user record with adv data
            try:
                await self._db.upsert_contact(pubkey_prefix, full_key, name, contact_type,
                                              last_hops=last_hops)
            except Exception as e:
                logger.warning(f"upsert_contact failed for {pubkey_prefix}: {e}")

        except Exception as e:
            logger.warning(f"Advertisement handler error: {e}", exc_info=True)

    # -- Inbound message handlers ----------------------------------------------

    async def _is_duplicate(self, sender_id: str, ts: int) -> bool:
        """
        Return True if we've seen this (sender_id, sender_timestamp) before.
        Checks in-memory cache first, then SQLite for post-restart replays.
        """
        key = f"{sender_id}:{ts}"
        now = time.time()
        cutoff = now - self._dedup_window

        if key in self._dedup_mem:
            return True

        if self._db:
            try:
                row = await self._db.fetchone(
                    "SELECT ts FROM _dedup WHERE key=?", (key,)
                )
                if row:
                    self._dedup_mem[key] = row["ts"]
                    return True
            except Exception as e:
                logger.debug(f"Dedup DB read error: {e}")

        self._dedup_mem[key] = now
        if self._db:
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO _dedup (key, ts) VALUES (?,?)", (key, now)
                )
                await self._db.commit()
            except Exception as e:
                logger.debug(f"Dedup DB write error: {e}")

        self._dedup_mem = {k: v for k, v in self._dedup_mem.items() if v > cutoff}
        return False

    async def _prune_dedup_db(self):
        """Periodically remove old entries from the dedup table."""
        if not self._db:
            return
        try:
            cutoff = time.time() - self._dedup_window
            await self._db.execute("DELETE FROM _dedup WHERE ts < ?", (cutoff,))
            await self._db.commit()
        except Exception as e:
            logger.debug(f"Dedup prune error: {e}")

    async def _ack_contact_msg(self, payload: dict):
        """No-op placeholder -- ACK is handled at firmware/transport layer in 2.x."""
        pass

    async def _on_contact_msg(self, event):
        """Handle a direct message from a contact."""
        try:
            payload = event.payload
            content = payload.get("text") or payload.get("content") or ""
            if not content:
                return

            sender_id   = str(payload.get("pubkey_prefix") or payload.get("sender_id") or "unknown")
            sender_name = _sanitise_name(payload.get("name") or payload.get("sender_name"))
            msg_ts      = int(payload.get("sender_timestamp") or payload.get("timestamp") or time.time())

            if await self._is_duplicate(sender_id, msg_ts):
                logger.warning(
                    f"Duplicate DM dropped: sender={sender_id} name={sender_name} "
                    f"sender_timestamp={msg_ts} content={content[:60]!r}"
                )
                return

            who = await self._db.format_user(sender_id, sender_name)
            logger.info(
                f"DM received: {who} ts={msg_ts} hops={payload.get('path_len')} "
                f"content={content[:60]!r}"
            )

            msg = Message(
                sender_id=sender_id,
                sender_name=sender_name,
                content=content.strip(),
                channel=None,
                ts=msg_ts,
                raw=payload,
            )
            asyncio.create_task(self.dispatcher.handle(msg))
        except Exception as e:
            logger.error(f"Error handling contact message: {e}", exc_info=True)

    async def _on_channel_msg(self, event):
        """
        Handle a channel (broadcast) message.

        Matching: channel_idx (integer slot number) is the primary key.
        channel_name from payload is logged for diagnostics but not used
        for filtering -- it may not be present in all firmware versions.

        Content stripping: MeshCore firmware prepends the sender's display
        name to channel message text as "DisplayName: <text>" before the
        library delivers it. We strip this prefix so the command parser
        sees the raw command. The name is extracted from the prefix if the
        payload doesn't carry a dedicated sender name field.

        Messages from unknown slots are logged at INFO so operators can
        see what's arriving and decide whether to enumerate (via !channel sync).
        """
        try:
            payload = event.payload
            raw_content = payload.get("text") or payload.get("content") or ""
            if not raw_content:
                return

            # Raw payload logged at DEBUG -- field names confirmed from live logs.
            logger.debug(f"Channel msg raw payload: {payload!r}")

            sender_id    = str(
                payload.get("pubkey_prefix") or
                payload.get("sender_id") or
                payload.get("from") or
                payload.get("src") or
                "unknown"
            )
            if sender_id == "unknown":
                logger.debug(
                    f"Channel msg sender_id unresolved -- payload keys: {list(payload.keys())!r} "
                    f"full payload: {payload!r}"
                )
            sender_name  = _sanitise_name(payload.get("name") or payload.get("sender_name") or payload.get("from_name"))
            msg_ts       = int(payload.get("sender_timestamp") or payload.get("timestamp") or time.time())
            channel_idx  = payload.get("channel_idx", payload.get("channel"))
            channel_name = payload.get("channel_name", "")

            # Strip "DisplayName: " prefix that MeshCore firmware prepends to
            # all channel messages. Strategy:
            #   1. If sender_name is known, look for "sender_name: " at the start.
            #   2. Otherwise, look for any "Word(s) emoji/punct: " prefix pattern.
            #   3. Extract the name from the prefix if sender_name is not in payload.
            content = _strip_channel_name_prefix(raw_content, sender_name)

            # If we didn't have sender_name from the payload but stripped a prefix,
            # use the stripped prefix as the display name.
            if not sender_name and content != raw_content:
                # The prefix that was stripped
                prefix_len = len(raw_content) - len(content)
                sender_name = _sanitise_name(raw_content[:prefix_len].rstrip(": ").strip())

            # Normalise idx to int for dict lookup; keep original for logging
            try:
                idx_int = int(channel_idx) if channel_idx is not None else None
            except (TypeError, ValueError):
                idx_int = None

            display = channel_name or (f"ch{idx_int}" if idx_int is not None else "ch?")

            if idx_int is None or not self._channel_is_known(idx_int):
                logger.info(
                    f"Channel msg from unknown slot -- "
                    f"channel_idx={channel_idx!r} channel_name={channel_name!r} "
                    f"sender={sender_id} content={content[:40]!r} "
                    f"(run '!channel sync' to enumerate radio channels)"
                )
                return

            if await self._is_duplicate(sender_id, msg_ts):
                logger.warning(
                    f"Duplicate channel msg dropped: sender={sender_id} "
                    f"channel={display} ts={msg_ts} content={content[:60]!r}"
                )
                return

            who = await self._db.format_user(sender_id, sender_name)
            logger.info(
                f"Channel msg: {who} channel={display} idx={idx_int} "
                f"hops={payload.get('path_len')} "
                f"raw={raw_content[:40]!r} content={content[:40]!r}"
            )

            respond_in_channel = self._channel_should_respond(idx_int)

            # If respond=False and sender is unknown we cannot reply at all --
            # drop cleanly rather than misrouting as a DM to "unknown".
            if not respond_in_channel and sender_id == "unknown":
                logger.debug(
                    f"Channel msg dropped -- respond=False and sender unknown: "
                    f"channel={display} content={content[:40]!r}"
                )
                return

            msg = Message(
                sender_id=sender_id,
                sender_name=sender_name,
                content=content.strip(),
                channel=display,
                ts=msg_ts,
                raw=payload,
            )
            asyncio.create_task(self.dispatcher.handle(msg))
        except Exception as e:
            logger.error(f"Error handling channel message: {e}", exc_info=True)

    # -- Outbound reply drain --------------------------------------------------

    async def _prune_contacts(self):
        """
        Prune the radio contact list when it exceeds contact_max.

        Holds _contacts_lock for the full duration so that a concurrent
        _refresh_contacts call (which skips when the lock is held) cannot
        repopulate self._contacts while removes are in flight.
        """
        if self._contacts_lock.locked():
            logger.debug("_prune_contacts: skipping -- refresh in progress")
            return

        async with self._contacts_lock:
            await self._do_prune_contacts()

    async def _do_prune_contacts(self):
        """Inner prune logic -- must be called with _contacts_lock held."""
        contact_max    = int(self.config.get("bot.contact_max",    300))
        contact_target = int(self.config.get("bot.contact_target", 250))

        total = len({k: v for k, v in self._contacts.items() if len(k) > 12})
        logger.info(f"Contact prune check: {total} contacts (max={contact_max} target={contact_target})")

        if total < contact_max:
            return

        need_to_remove = total - contact_target
        logger.warning(
            f"Contact list at {total} -- pruning {need_to_remove} contacts down to {contact_target}"
        )
        removed = 0

        # Build full_key -> contact dict (skip prefix aliases -- len > 12 = full key)
        full_contacts = {k: v for k, v in self._contacts.items() if len(k) > 12}

        # -- Pass 0: non-Chat-User contacts (type != 1) -----------------------
        # Repeaters, room servers, sensors etc. are pruned first -- they don't
        # interact with the bot and can always re-add themselves by advertising.
        if removed < need_to_remove:
            try:
                rows = await self._db.fetchall(
                    """SELECT pubkey_prefix, full_public_key, contact_type
                       FROM users
                       WHERE contact_type IS NOT NULL AND contact_type != 1
                         AND full_public_key IS NOT NULL
                       ORDER BY last_advert_ts ASC"""
                )
            except Exception as e:
                logger.warning(f"Prune pass 0 DB query failed: {e}")
                rows = []

            for row in rows:
                if removed >= need_to_remove:
                    break
                full_key = row["full_public_key"]
                # Skip contacts not in the live cache -- they were already
                # removed from the radio (e.g. manually or by a prior prune)
                # and only remain as DB history rows. Calling remove_contact
                # on a ghost entry wastes a serial round-trip and counts
                # against need_to_remove without actually freeing a slot.
                if full_key not in self._contacts:
                    continue
                if await self._remove_contact(
                    full_key, row["pubkey_prefix"],
                    reason=f"type={row['contact_type']} (non-chat-user)"
                ):
                    removed += 1

        # -- Pass 1: never-upserted contacts, ordered by last_advert ASC ------
        if removed < need_to_remove:
            known_prefixes = set()
            try:
                rows = await self._db.fetchall("SELECT pubkey_prefix FROM users")
                known_prefixes = {r["pubkey_prefix"] for r in rows}
            except Exception as e:
                logger.warning(f"Prune pass 1 DB query failed: {e}")

            # Contacts not in users table at all
            unknown = []
            for full_key, contact in full_contacts.items():
                prefix = full_key[:12]
                if prefix not in known_prefixes:
                    last_adv = contact.get("last_advert") or 0
                    unknown.append((last_adv, full_key, prefix))
            unknown.sort(key=lambda x: x[0])  # oldest last_advert first

            for last_adv, full_key, prefix in unknown:
                if removed >= need_to_remove:
                    break
                if await self._remove_contact(full_key, prefix, reason="never seen, pass 1"):
                    removed += 1

        # -- Pass 2: priv=1 users, ordered by last_seen_ts ASC ----------------
        # Refresh snapshot so pass 0 removals are reflected -- without this,
        # contacts removed in pass 0 would still appear in full_contacts and
        # pass 2 would re-attempt removing them from the radio, wasting removes.
        full_contacts = {k: v for k, v in self._contacts.items() if len(k) > 12}
        if removed < need_to_remove:
            try:
                rows = await self._db.fetchall(
                    """SELECT pubkey_prefix, full_public_key, last_seen_ts
                       FROM users
                       WHERE privilege = 1 AND full_public_key IS NOT NULL
                       ORDER BY last_seen_ts ASC"""
                )
            except Exception as e:
                logger.warning(f"Prune pass 2 DB query failed: {e}")
                rows = []

            for row in rows:
                if removed >= need_to_remove:
                    break
                full_key = row["full_public_key"]
                prefix   = row["pubkey_prefix"]
                if full_key in full_contacts:
                    if await self._remove_contact(
                        full_key, prefix,
                        reason=f"priv=1 last_seen={row['last_seen_ts']}"
                    ):
                        removed += 1

        logger.info(f"Contact prune complete: removed {removed} contacts")

    async def _remove_contact(self, full_key: str, prefix: str,
                               reason: str = "") -> bool:
        """
        Remove a contact from the radio contact list.
        Returns True on success, False on failure.
        """
        try:
            await self._mc.commands.remove_contact(full_key)

            # The radio processes remove_contact asynchronously. Without a brief
            # delay, a subsequent get_contacts() call (e.g. from _refresh_contacts
            # triggered by an ADV event or the 30s run loop) can race the firmware
            # and return a stale list that still includes the just-removed contact,
            # causing it to be re-added to self._contacts. 0.25s confirmed sufficient
            # in live testing on NRF52840 serial firmware.
            await asyncio.sleep(0.25)

            # Remove from local cache
            self._contacts.pop(full_key, None)
            self._contacts.pop(prefix, None)
            logger.info(
                f"Contact removed: prefix={prefix} full={full_key[:16]}..."
                + (f" reason={reason}" if reason else "")
            )
            return True
        except Exception as e:
            logger.warning(f"remove_contact failed for {prefix}: {e}")
            return False

    def get_radio_contact_count(self) -> int:
        """
        Return the current number of contacts in the live radio contact cache.
        Uses the same expression as _prune_contacts -- full-length keys only,
        excluding the 12-char prefix aliases stored alongside each contact.
        Safe to call from any coroutine; reads self._contacts without locking
        (the dict is only mutated from _refresh_contacts, which is serialised
        by _contacts_lock, so a momentary read here is consistent enough for
        dashboard display purposes).
        """
        return len({k: v for k, v in self._contacts.items() if len(k) > 12})

    async def get_node_info(self) -> dict:
        """
        Query the radio for a comprehensive snapshot of node state.
        Fetches on-demand (never cached) so callers always get current values --
        important since settings may change via !node set or the radio UI.

        Returns a dict with keys:
          self_info   -- send_appstart payload (radio config, name, pubkey)
          battery     -- get_bat payload (level_mv, used_kb, total_kb)
          telemetry   -- get_self_telemetry payload (voltage)
          stats_core  -- get_stats_core payload (uptime_secs, errors, queue_len)
          stats_radio -- get_stats_radio payload (noise_floor, rssi, snr, air times)
          stats_packets -- get_stats_packets payload (recv, sent, flood/direct, errors)
          autoadd     -- get_autoadd_config payload (config bitmask)
          contacts    -- live contact type breakdown from self._contacts cache
          error       -- set to an error message string if any call fails

        All sub-calls are attempted independently; a failure in one does not
        prevent the others from populating.
        """
        result = {}

        async def _try(key, coro):
            try:
                evt = await coro
                result[key] = evt.payload
            except Exception as e:
                result[key] = None
                result.setdefault("errors", {})[key] = str(e)

        await _try("self_info",     self._mc.commands.send_appstart())
        await _try("battery",       self._mc.commands.get_bat())
        await _try("telemetry",     self._mc.commands.get_self_telemetry())
        await _try("stats_core",    self._mc.commands.get_stats_core())
        await _try("stats_radio",   self._mc.commands.get_stats_radio())
        await _try("stats_packets", self._mc.commands.get_stats_packets())
        await _try("autoadd",       self._mc.commands.get_autoadd_config())

        # Contact type breakdown from live cache (no serial call needed)
        by_type = {}
        for k, v in self._contacts.items():
            if len(k) > 12:  # full keys only, skip prefix aliases
                t = v.get("type")
                key = f"type{t}" if t is not None else "type?"
                by_type[key] = by_type.get(key, 0) + 1
        result["contacts"] = by_type

        return result

    async def send_advertisement(self) -> bool:
        """
        Broadcast a self-advertisement to the mesh immediately.
        Returns True on success, False if no adv method available or call fails.
        Called by the !advertise admin command.
        """
        if not self._adv_method:
            logger.warning("send_advertisement() called but no adv method available.")
            return False
        try:
            await self._adv_method()
            logger.info("Bot advertisement sent (on-demand).")
            return True
        except Exception as e:
            logger.warning(f"Bot advertisement failed: {e}")
            return False

    async def _reply_drain_loop(self):
        pace = self.config.get("connection.reply_pace_seconds", 1.5)
        while self._running:
            try:
                item = await asyncio.wait_for(
                    self.dispatcher.reply_queue.get(), timeout=1.0
                )
                if self._mc:
                    await self._send_reply(item)
                else:
                    logger.warning("Reply dropped -- not connected.")
                self.dispatcher.reply_queue.task_done()
                await asyncio.sleep(pace)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Reply drain error: {e}")

    async def _send_reply(self, item: dict):
        """Send a reply DM or channel message."""
        try:
            text = item["text"]
            if item.get("total", 1) > 1:
                text = f"[{item['part']}/{item['total']}]\n{text}"

            if item.get("channel"):
                logger.info(
                    f"SEND channel={item['channel']!r} "
                    f"idx={item.get('channel_idx')!r} "
                    f"part={item.get('part', 1)}/{item.get('total', 1)} "
                    f"len={len(text)} text={text[:60]!r}"
                )
                await self._send_to_channel(item["channel"], item.get("channel_idx"), text)
            else:
                contact = self._find_contact(item["target_id"])
                if contact:
                    logger.info(
                        f"SEND DM to={item['target_id']} "
                        f"part={item.get('part', 1)}/{item.get('total', 1)} "
                        f"len={len(text)} text={text[:60]!r}"
                    )
                    await self._mc.commands.send_msg(contact, text)
                else:
                    logger.warning(
                        f"Cannot send DM -- contact not found for {item['target_id']}. "
                        "Node may not be in contacts yet."
                    )
        except Exception as e:
            logger.error(f"Send error: {e}", exc_info=True)

    async def _send_to_channel(self, channel_label: str, channel_idx, text: str):
        """
        Send a message to a channel slot.
        Confirmed API name from meshcore_py: send_chan_msg(channel_idx, text).
        Falls back to send_channel_msg / send_channel_message for older versions.
        """
        try:
            if hasattr(self._mc.commands, "send_chan_msg"):
                logger.debug(f"send_chan_msg({channel_idx!r}, ...)")
                await self._mc.commands.send_chan_msg(channel_idx, text)
            elif hasattr(self._mc.commands, "send_channel_msg"):
                logger.debug(f"send_channel_msg({channel_idx!r}, ...)")
                await self._mc.commands.send_channel_msg(channel_idx, text)
            elif hasattr(self._mc.commands, "send_channel_message"):
                logger.debug(f"send_channel_message({channel_idx or channel_label!r}, ...)")
                await self._mc.commands.send_channel_message(channel_idx or channel_label, text)
            else:
                # Log all available commands so we can find the right name
                available = [m for m in dir(self._mc.commands) if not m.startswith("_")]
                logger.warning(
                    f"No channel send method found. Available commands: {available}\n"
                    f"Message to '{channel_label}' (idx={channel_idx}) dropped."
                )
        except Exception as e:
            logger.error(f"Channel send error: {e}", exc_info=True)

    def _find_contact(self, sender_id: str):
        """Look up a contact object by pubkey_prefix (12-char) or full public_key."""
        if not self._contacts:
            return None
        if sender_id in self._contacts:
            return self._contacts[sender_id]
        for key, contact in self._contacts.items():
            if str(key).startswith(sender_id):
                return contact
        return None

    async def _get_channel_by_name(self, name: str):
        """
        Provider for dispatcher.get_channel_by_name().
        Looks up a channel row from the _channels DB table by name (case-insensitive).
        Returns a dict with {channel_idx, name, respond, disabled_at} or None.
        """
        try:
            row = await self._db.fetchone(
                "SELECT channel_idx, name, respond, disabled_at FROM _channels "
                "WHERE lower(name)=lower(?)",
                (name,),
            )
            if row is None:
                return None
            return dict(row)
        except Exception as e:
            logger.warning(f"_get_channel_by_name({name!r}) error: {e}")
            return None

    def _get_contacts_snapshot(self) -> dict:
        """
        Provider for dispatcher.get_contacts_snapshot().
        Returns a shallow copy of the in-memory contacts cache.
        Safe to call from any coroutine -- reads without locking
        (consistent enough for display purposes; same caveat as get_radio_contact_count).
        """
        return dict(self._contacts)
