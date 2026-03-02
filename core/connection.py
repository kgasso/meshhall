"""
ConnectionManager — wraps the meshcore library using its actual API.

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
    the RF packet — no application-level ACK is needed or possible in 2.x.
    Duplicates are caused by the node replaying buffered messages on bot
    reconnect. Dedup is persisted to SQLite so restarts don't re-execute
    buffered commands.

Channel architecture:
    Channels are enumerated from the radio at startup and rehash via
    CMD_GET_CHANNEL (slot 0-7). The channel name, respond flag, and a
    safety disabled_at timestamp are stored in the _channels DB table.
    No channel configuration lives in config.yaml — the radio is the
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

# ── DB schemas registered at startup ──────────────────────────────────────────

# Dedup table — survives bot restarts so buffered message replay on reconnect
# doesn't re-execute commands.
DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS _dedup (
    key         TEXT PRIMARY KEY,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedup_ts ON _dedup(ts);
"""

# Channel table — one row per non-empty radio slot, keyed by channel_idx.
# respond=1 means the bot will reply into the channel; 0 = listen-only.
# disabled_at is set (epoch ts) when the bot auto-disables a slot because
# the channel name changed since last enumeration — safety guard against
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
      2. Otherwise, strip any "words: " prefix — defined as one or more
         non-colon tokens (which may include emoji/punctuation) followed by
         ": " — as long as what remains is non-empty.

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

    # Strategy 2: pattern match — "anything up to first ': '" where the
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


class ConnectionManager:
    def __init__(self, config, dispatcher: Dispatcher, db=None):
        self.config = config
        self.dispatcher = dispatcher
        self._db = db
        self._running = False
        self._mc = None
        self._contacts = {}

        # In-memory channel state rebuilt from DB after every enumeration.
        # Keys are channel_idx integers; values are dicts with name/respond/disabled_at.
        self._channels: dict = {}

        # Deduplication — in-memory cache backed by SQLite for restart persistence.
        # Key: "{sender_id}:{sender_timestamp}"
        # Value: time.time() when first seen
        self._dedup_mem: dict = {}
        self._dedup_window: float = config.get("connection.dedup_window_seconds", 120.0)

        if db:
            db.register_schema(DEDUP_SCHEMA)
            db.register_schema(CHANNEL_SCHEMA)

    async def run(self):
        """
        Run the bot until stop() is called.

        Uses asyncio.gather() with two long-running coroutines. stop() cancels
        the gather task, which causes run() to return normally. main() then
        returns normally, and asyncio.run() exits with code 0 — no sys.exit(),
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
            pass  # clean shutdown via stop() — not an error

    async def stop(self):
        """
        Signal the bot to stop and wait for cleanup.

        Sets _running=False (stops retry loops), disconnects the radio,
        then cancels the gather task so run() returns. Awaited by shutdown
        coroutines — after this returns, main() can return and asyncio.run()
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

    # ── Connection with auto-reconnect ────────────────────────────────────────

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

        # Populate contacts cache and seed user registry — verbose on startup
        await self._refresh_contacts(verbose=True)

        # Subscribe to direct messages (contact → bot)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

        # Subscribe to channel messages
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)

        # Subscribe to advertisements and new contacts.
        # Probe defensively — event types vary across 2.x versions.
        for evt_name in ("ADVERTISEMENT", "NEW_CONTACT", "CONTACT_UPDATE"):
            evt = getattr(EventType, evt_name, None)
            if evt is not None:
                self._mc.subscribe(evt, self._on_advertisement)
                logger.info(f"Subscribed to EventType.{evt_name} for display name tracking")
            else:
                logger.info(
                    f"EventType.{evt_name} not available in this meshcore version — "
                    "display names will be populated from contacts cache and inbound messages only"
                )

        # Log all available EventTypes at startup for diagnostics
        all_events = [e for e in dir(EventType) if not e.startswith("_")]
        logger.info(f"Available EventTypes: {all_events}")

        # Probe for self-advertisement capability
        adv_method = None
        for method_name in ("send_advertise", "advertise", "send_adv", "send_advertisement"):
            if hasattr(self._mc.commands, method_name):
                adv_method = getattr(self._mc.commands, method_name)
                logger.info(f"Bot advertisement: using commands.{method_name}()")
                break
        if adv_method is None:
            logger.info(
                "Bot advertisement: no send_advertise method found in this "
                "meshcore version — bot will not self-advertise"
            )

        # Start background message polling
        await self._mc.start_auto_message_fetching()
        logger.info("Subscribed to messages. Bot is live.")

        # Enumerate channel slots from the radio and reconcile with DB
        await self.enumerate_channels()

        # Keep the connection alive until disconnected
        prune_interval  = 3600
        last_prune      = time.time()
        adv_interval    = self.config.get("bot.advertise_interval", 0)
        last_adv        = 0.0   # send first advert immediately if enabled

        while self._running:
            await asyncio.sleep(30)

            # Periodic contacts refresh (quiet — only logs changes)
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

            # Dedup DB prune
            if time.time() - last_prune > prune_interval:
                await self._prune_dedup_db()
                last_prune = time.time()

    # ── Channel enumeration ───────────────────────────────────────────────────

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
        existing DB state — no slots are disabled purely due to read failure.

        Returns a human-readable summary string (used by !channel sync and
        in startup logs).
        """
        if not self._mc:
            return "Not connected to radio — channel enumeration skipped."

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
            return "No get_channel API available — using cached DB state."

        now = int(time.time())
        new_slots = []
        changed_slots = []
        seen_slots = []

        for idx in range(MAX_CHANNEL_SLOTS):
            try:
                result = await get_ch_fn(idx)
            except Exception as e:
                logger.warning(f"get_channel({idx}) failed: {e}")
                continue

            # Extract name from result — handle both dict payload and attribute access
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
                logger.debug(f"Channel slot {idx}: empty — skipping")
                continue

            # Reconcile with DB
            existing = await self._db.fetchone(
                "SELECT name, respond, disabled_at FROM _channels WHERE channel_idx=?", (idx,)
            )

            if existing is None:
                # New slot — add with respond=0
                await self._db.execute(
                    "INSERT INTO _channels (channel_idx, name, respond, last_seen, disabled_at) "
                    "VALUES (?, ?, 0, ?, NULL)",
                    (idx, name, now),
                )
                await self._db.commit()
                logger.info(f"Channel slot {idx}: new — name={name!r} respond=off")
                new_slots.append(f"[{idx}] {name} (new, respond=off)")

            elif existing["name"] != name:
                # Name changed — auto-disable and warn
                await self._db.execute(
                    "UPDATE _channels SET name=?, respond=0, last_seen=?, disabled_at=? "
                    "WHERE channel_idx=?",
                    (name, now, now, idx),
                )
                await self._db.commit()
                logger.warning(
                    f"Channel slot {idx}: name changed {existing['name']!r} → {name!r}. "
                    f"Respond DISABLED (was {'on' if existing['respond'] else 'off'}). "
                    f"Use '!channel {idx} on' to re-enable after verifying."
                )
                changed_slots.append(
                    f"[{idx}] {existing['name']!r}→{name!r} DISABLED"
                )

            else:
                # Same name — just touch last_seen
                await self._db.execute(
                    "UPDATE _channels SET last_seen=? WHERE channel_idx=?",
                    (now, idx),
                )
                await self._db.commit()
                seen_slots.append(idx)

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
        if not parts:
            parts.append("No channel slots found on radio.")

        summary = "Channel sync: " + "; ".join(parts)
        logger.info(summary)
        return summary

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

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def _refresh_contacts(self, verbose: bool = False):
        """
        Fetch contacts from node and upsert into user registry.

        Key facts confirmed from live logs:
          - contacts payload is a dict keyed by full 64-char public_key
          - each value is a dict with: public_key, adv_name, last_advert, etc.
          - pubkey_prefix (12-char) = first 12 chars of public_key
          - ADVERTISEMENT events only carry public_key — no name — so we
            refresh contacts after each advertisement to pick up adv_name
        """
        try:
            from meshcore import EventType
            result = await self._mc.commands.get_contacts()

            if result.type == EventType.ERROR:
                logger.warning(f"get_contacts() returned ERROR: {result.payload}")
                return

            payload = result.payload
            if not payload or not isinstance(payload, dict):
                if verbose:
                    logger.info(f"get_contacts() empty or unexpected: {type(payload).__name__}")
                return

            seeded = 0
            updated = 0
            for full_key, contact in payload.items():
                if not isinstance(contact, dict):
                    continue

                pubkey_prefix = str(full_key)[:12]

                # Skip malformed or missing keys — don't create/update a shared
                # "unknown" row that would thrash with every contact refresh.
                if not pubkey_prefix or pubkey_prefix == "unknown":
                    logger.debug(f"_refresh_contacts: skipping contact with bad key {full_key!r}")
                    continue

                name = (contact.get("adv_name") or contact.get("name")
                        or contact.get("display_name"))

                self._contacts[str(full_key)] = contact
                self._contacts[pubkey_prefix] = contact  # alias for fast lookup

                if verbose:
                    logger.info(
                        f"Contact: prefix={pubkey_prefix} full={str(full_key)[:16]}... "
                        f"name={name!r} last_advert={contact.get('last_advert')}"
                    )

                try:
                    existing = await self._db.get_user(pubkey_prefix)
                    await self._db.upsert_user(pubkey_prefix, name)
                    if existing is None:
                        seeded += 1
                        logger.info(f"New contact: {pubkey_prefix} ({name!r})")
                    elif name and name != existing["display_name"]:
                        updated += 1
                        logger.info(
                            f"Name updated: {pubkey_prefix} "
                            f"{existing['display_name']!r} → {name!r}"
                        )
                except Exception as e:
                    logger.warning(f"upsert_user failed for {pubkey_prefix}: {e}")

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
        The payload only contains public_key — refresh contacts to get adv_name.
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

            pubkey_prefix = str(full_key)[:12] if full_key else ""
            logger.info(
                f"ADV received: prefix={pubkey_prefix} "
                f"full={str(full_key)[:16]}{'...' if len(str(full_key)) > 16 else ''}"
            )
            await self._refresh_contacts()

        except Exception as e:
            logger.warning(f"Advertisement handler error: {e}", exc_info=True)

    # ── Inbound message handlers ──────────────────────────────────────────────

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
        """No-op placeholder — ACK is handled at firmware/transport layer in 2.x."""
        pass

    async def _on_contact_msg(self, event):
        """Handle a direct message from a contact."""
        try:
            payload = event.payload
            content = payload.get("text") or payload.get("content") or ""
            if not content:
                return

            sender_id   = str(payload.get("pubkey_prefix") or payload.get("sender_id") or "unknown")
            sender_name = payload.get("name") or payload.get("sender_name")
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
        for filtering — it may not be present in all firmware versions.

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

            # Raw payload logged at DEBUG — field names confirmed from live logs.
            logger.debug(f"Channel msg raw payload: {payload!r}")

            sender_id    = str(
                payload.get("pubkey_prefix") or
                payload.get("sender_id") or
                payload.get("from") or
                payload.get("src") or
                "unknown"
            )
            sender_name  = payload.get("name") or payload.get("sender_name") or payload.get("from_name")
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
                sender_name = raw_content[:prefix_len].rstrip(": ").strip()

            # Normalise idx to int for dict lookup; keep original for logging
            try:
                idx_int = int(channel_idx) if channel_idx is not None else None
            except (TypeError, ValueError):
                idx_int = None

            display = channel_name or (f"ch{idx_int}" if idx_int is not None else "ch?")

            if idx_int is None or not self._channel_is_known(idx_int):
                logger.info(
                    f"Channel msg from unknown slot — "
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
            msg = Message(
                sender_id=sender_id,
                sender_name=sender_name,
                content=content.strip(),
                channel=display if respond_in_channel else None,
                ts=msg_ts,
                raw=payload,
            )
            asyncio.create_task(self.dispatcher.handle(msg))
        except Exception as e:
            logger.error(f"Error handling channel message: {e}", exc_info=True)

    # ── Outbound reply drain ──────────────────────────────────────────────────

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
                    logger.warning("Reply dropped — not connected.")
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
                logger.debug(
                    f"Sending channel reply: channel={item['channel']!r} "
                    f"idx={item.get('channel_idx')!r} text={text[:40]!r}"
                )
                await self._send_to_channel(item["channel"], item.get("channel_idx"), text)
            else:
                contact = self._find_contact(item["target_id"])
                if contact:
                    logger.debug(f"Sending DM reply to {item['target_id']} text={text[:40]!r}")
                    await self._mc.commands.send_msg(contact, text)
                else:
                    logger.warning(
                        f"Cannot send DM — contact not found for {item['target_id']}. "
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
