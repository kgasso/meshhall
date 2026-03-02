# MeshHall Changelog

All notable changes to MeshHall are documented here.
Format: `[core vX.Y.Z]` for core changes, `[plugin vX.Y.Z]` for plugin changes.


## Known Enhancements / Future Work

- **Logging:** Currently writes to both `stdout` (captured by systemd journal via
  `StandardOutput=journal`) and a flat file (`data/meshhall.log`). The flat file
  has no rotation and will grow indefinitely. Options: drop the file handler in
  favour of journal-only, or replace `FileHandler` with `RotatingFileHandler`.
  For now the file is a useful `tail -f` fallback; revisit when disk management
  becomes a concern.

---

## [v0.8.0] — 2026-03-01

### Core v0.8.0
- **`!restart` command:** Admin-only, DM-only. Sends a disruptive-action warning
  and requires `!restart confirm` within 60 seconds to proceed. On confirmation,
  drains the reply queue (up to 10s), then re-execs the bot process via
  `os.execv` — systemd restarts it automatically per the service `Restart=`
  policy.
- **`!shutdown` command:** Same confirmation flow as `!restart`. On confirmation,
  sends SIGTERM to itself, which triggers the existing graceful shutdown handler
  (disconnects radio, closes DB).
- **Confirmation system:** `Dispatcher._pending_confirm` dict tracks pending
  disruptive action requests per sender with a 60-second TTL. Expired
  confirmations are rejected with a helpful message. Confirmation state is
  in-memory only — a bot restart clears all pending confirmations.
- **System action callback:** `dispatcher.set_system_action_callback()` decouples
  OS-level actions from the dispatcher. Registered in `meshhall.py` after the
  event loop starts; the dispatcher calls it after the reply queue drains.
- **Rate limiting:** New `core/ratelimit.py` — token bucket rate limiter for
  channel commands. Per-sender-per-channel bucket (capacity 4, refill 0.1/s)
  and per-channel bucket (capacity 10, refill 0.25/s). Channel bucket exhaustion
  → silent drop + WARNING log. Sender bucket exhaustion → single warning reply
  with retry estimate, then silent drop. DMs always exempt. Config under
  `channels.rate_limit` in `config.yaml`.
- **Unknown sender handling:** `handle()` no longer calls `upsert_user` when
  `sender_id` is `"unknown"` (channel messages with no pubkey in payload).
  Previously, the shared `"unknown"` DB row had its display name overwritten by
  every channel sender, causing log noise and incorrect user registry entries.
  Unknown senders receive `PRIV_DEFAULT` and cannot be muted or privileged.
- **Welcome message:** On first DM (or after `bot.intro_window_minutes`, default
  60), the bot sends a configurable welcome: `"Hi, I'm <bot.name> - a MeshHall
  bot. Use !help for assistance."` Set `intro_window_minutes: 0` for first
  contact only. Tracked in new `users.welcomed_ts` DB column.
- **Bot name in responses:** `!about` now uses `bot.name` from config rather
  than the hardcoded string `"MeshHall"`. Format:
  `<bot.name> - a MeshHall bot - meshhall.org`.
- **`!help` in channel:** When `!help` is called from a channel, the bot replies
  with a short nudge ("DM <bot.name> with !help for the full command list.")
  instead of flooding the channel with the full command list.
- **Command scope corrections:** Several commands moved to `scope="direct"` to
  reduce channel noise — `!about`, `!channels`, `!checkin`, `!roll`, `!bulletins`,
  `!freqs`. Commands that remain channel-scoped: `!ping`, `!time`, `!wx`,
  `!alerts`, `!help` (though limited).
- **Database migration:** Added `ALTER TABLE` migration runner in
  `Database.initialize()` for adding columns to existing deployments without
  requiring a full schema drop. First migration adds `users.welcomed_ts`.
- **Bugfix:** Dead code block removed from `Database.format_user()`.
- **Bugfix:** `_refresh_contacts` now skips contact entries with empty or
  `"unknown"` pubkey prefix, preventing the `"unknown"` user row from being
  updated with real contact names.

### Plugins
- **`time` v0.2.0:** `!time` now shows the OS timezone abbreviation (e.g.
  PST, PDT) instead of the word "local". Uses `datetime.astimezone()` which
  respects the system timezone set on the Pi.
- **`channels` v0.1.0:** `!channels` scope changed to `direct`.

### Config
- `bot.intro_window_minutes` added (default 60).
- `channels.rate_limit` defaults updated: `per_sender.capacity` 4,
  `per_channel.capacity` 10, `per_channel.refill_rate` 0.25.
- TCP connection note updated to indicate it is untested and likely non-functional
  without a compatible ESP32-based device.

---

## [v0.7.0] — 2026-02-28

### Core v0.7.0
- **SAME service migration:** The MeshHall-SAME service has been migrated into
  its own codebase to allow for deployment only if needed. Available on Github
  at https://github.com/kgasso/meshhall-same - still uses the main MeshHall
  database for inserting alerts.
- **Chunk prefix newline:** Multi-part messages now format as `[1/4]\n<content>`
  instead of `[1/4] <content>`, preserving indentation on wrapped help output.
  `PREFIX_OVERHEAD` updated to 9 bytes to account for the newline.
- **Spaced command tolerance:** `! ping` is now equivalent to `!ping` — a space
  between the command character and command name is accepted. Handles mobile
  autocorrect inserting a space after `!`. Works with all command characters and
  argument forms (`! wx 4`, `/ help ping`, etc.).
- **Simplified `!help`:** Summary output is now a flat, one-line-per-command
  list with a preamble directing users to `!help <command>` for details.
  Category headers removed to reduce message count.
- **Context-sensitive `!help <cmd>`:** `!help ping` (or `!help !ping`, or
  `/help /ping` with alternate char) returns the command's full description,
  `Usage:` line if applicable, scope, and required privilege. Privilege check
  applied — denied commands return an access error rather than details.
- **`usage_text` field on `CommandEntry`:** All plugin commands updated with
  a separate `usage_text` parameter for extended help. Short `help_text` shown
  in summary; full `usage_text` shown in per-command help only.
- **`!about` command:** New core builtin (scope: channel, priv: default).
  Reads `bot.admin_name` and `bot.admin_contact` from config; outputs bot
  identity and operator contact info.
- **Config:** `bot.admin_name` and `bot.admin_contact` fields added with
  placeholder defaults.


---

## [v0.6.0] — Unreleased

### Core v0.6.0
- **Bugfix — ping:** `!ping` no longer says "hop(s)" when the path is a direct
  connection (path_len=255). Response is now `Pong! Direct` for direct,
  `Pong! N hop(s)` for relayed, `Pong! Path: unknown` when not reported.
- **Enhancement — custom command character:** Bot command prefix is now
  configurable via `bot.command_char` in `config.yaml` (default `!`).
  Any single non-alphanumeric, non-space character is valid (e.g. `/`, `.`, `#`).
  Takes effect immediately after `!rehash`. Plugins continue to register
  commands as `!cmd` internally — the dispatcher translates at dispatch time
  so plugins need no changes. Help output displays the configured character.
- **Feature — channel support:** Bot now responds on channels defined in
  `config.channels[]`. Each channel entry specifies a `name`, `channel_idx`
  (firmware slot), `open` (hashtag vs private), optional `key`, and `respond`
  flag. Bot probes for a channel join API at connect time (`set_channel`,
  `join_channel`, etc.) and calls it per channel; if no API is available,
  channel filtering still works based on inbound `channel_name`/`channel_idx`.
  Messages from unconfigured channels are silently ignored. Channels with
  `respond: false` are ignored.
- **Feature — database backup script:** `tools/backup_db.sh` — takes a safe
  online snapshot using SQLite's backup API while the bot runs. Supports
  `--db`, `--dest`, `--keep` flags and automatic pruning of old backups.
  `tools/BACKUPS.md` documents ad-hoc usage and cron scheduling examples.
- **Fix — dispatcher syntax error:** Removed a stray `return` statement
  introduced by a failed automated edit in the previous session.

---

## [v0.5.0] — Unreleased

### Core v0.5.0
- **Rename:** Project renamed from MeshBot to **MeshHall** throughout —
  filenames (`meshhall.py`), service names (`meshhall.service`,
  `meshhall-same.service`), install path (`/opt/meshhall`), database
  (`data/meshhall.db`), log (`data/meshhall.log`), service user (`meshhall`).
- **Version consolidation:** Core version is now the single source of truth in
  `core/__init__.py` (`__version__`). `meshhall.py` imports from there — no
  more keeping two constants in sync. Plugin versions use `__version__` (the
  standard Python dunder) so header metadata and `PluginLoader` read the same
  field.
- **Uninstall script:** `uninstall.sh` / `deploy/uninstall.sh` — stops and
  disables services, removes unit files, removes install directory and service
  user. Preserves `data/` (database + log) by default; `--delete-db` flag
  required to remove it. Supports `--yes` and `--install-dir` matching install.sh.
- **Authorship headers:** All plugin and core module files now carry
  `__author__`, `__email__`, `__copyright__`, `__license__`, and `__version__`
  headers per PEP 8 convention.
- **Fix:** Message chunking operates on UTF-8 byte length (156-byte firmware
  limit confirmed empirically). Previous 180-char limit caused silent truncation.

---

## [v0.4.1] — Unreleased

### Core v0.4.1
- **Fix:** Message chunking now operates on UTF-8 **byte** length (156-byte firmware
  limit) rather than character count. Emoji and other multibyte codepoints are
  counted correctly. Previous limit of 180 chars caused silent firmware truncation.
- **Fix:** `[part/total]` prefix overhead (8 bytes) is now reserved during chunking
  so the final sent string never exceeds the firmware limit.

---

## [v0.4.0] — Unreleased

### Core v0.4.0
- Added `CORE_VERSION` constant and `!version` command (admin DM) showing core
  version and all loaded plugins with their individual version strings.
- `PluginLoader` now collects `VERSION` from each plugin module.
- Command categories added to `CommandEntry`; `!help` output grouped as:
  Core first, then remaining categories alphabetically, commands within each
  sorted alphabetically.
- All log lines now use `key (Name)` format via `db.format_user()` — names
  populate from contacts cache so messages are traceable to callsigns.
- Default `advertise_interval` set to 3600s (1 hour).

### Plugin v0.4.0 — weather
- `!wx` now accepts an optional period count: `!wx [1-8]`, default 2.
- Rehash callback triggers immediate NWS re-fetch when zone/coords change.
- `!wxrefresh` admin command for on-demand NWS pull.
- Corrected NWS API endpoints (`api.weather.gov`; `alerts.weather.gov` was
  decommissioned December 2, 2025).

### Plugin v0.3.0 — checkin
- Station display uses `Name (hash)` format; falls back to hash-only.
- `!missing` and `!roll` use consistent `_display()` helper.

---

## [v0.3.0] — Unreleased

### Core v0.3.0
- Privilege system (0–15): muted/default/configurable tiers/admin.
- User registry (`users` table): auto-created on first contact at privilege 1,
  updated from contacts cache and advertisement events.
- `db.upsert_user()`, `db.get_user()`, `db.find_user()`, `db.set_privilege()`.
- Privilege resolved live at dispatch time from plugin config — `!rehash` picks
  up changes without restart. Hardcoded floor per command prevents over-permission.
- Command scope: `"direct"` (DM only) or `"channel"` (DM or channel).
- `!help` filtered by caller's privilege and context (DM vs channel).
- `!whoami` built-in: shows name, ID hash, privilege level.
- `!ping` built-in: returns hop count from `path_len` payload field.
- `!rehash` moved to dispatcher as built-in; requires privilege 15.
- Admin bootstrap: `bot.admins` list in `config.yaml` promoted to privilege 15
  on startup and on every `!rehash`.
- SIGHUP triggers rehash without restart.
- `bot.advertise_interval` config: periodic self-advertisement via
  `commands.send_advertise()` if available in the library.

### Plugin v0.2.0 — bulletin, frequencies, replay
- Privilege and scope system integrated.
- `!post` and `!delbul` require privilege 2 (known member).
- `!addfreq`/`!delfreq` require privilege 15 (admin).

### Plugin v0.1.0 — users (new)
- `!whois`, `!users`, `!setpriv`, `!mute`, `!unmute` commands.
- All require privilege 15 except `!whois` (privilege 1).

---

## [v0.2.0] — Unreleased

### Core v0.2.0
- Persistent deduplication via SQLite `_dedup` table — bot restart no longer
  re-executes commands buffered by the node.
- Dedup key: `sender_id:sender_timestamp` (set once by originator, identical
  across all retries of the same message).
- Dedup window configurable via `connection.dedup_window_seconds` (default 120s).
- Hot config reload: `config.reload()` re-reads all YAML files; plugin cache cleared.
- `dispatcher.register_rehash_callback()` for plugins needing post-reload work.
- Admin audit logging: `ADMIN CMD`, `ADMIN GRANTED`, `ADMIN DENIED` log lines.
- `dispatcher.log_admin_attempt()` helper for consistent plugin audit trails.
- Per-plugin config files: `config/plugins/<name>.yaml` loaded lazily via
  `config.plugin("name")` — changes picked up on `!rehash`.

### Plugin v0.1.1 — time
- Lazy timezone reading; respects `!rehash`.

---

## [v0.1.0] — Unreleased

### Core v0.1.0
- Initial release: plugin architecture, async SQLite via `aiosqlite`, modular
  config, message logging, reply chunking, reply pacing.
- Plugin loader: alphabetical load order, `setup(dispatcher, config, db)` API.
- Startup sequence: config → DB object → dispatcher → plugins (schemas registered)
  → ConnectionManager (dedup schema) → `db.initialize()` (all tables created).
- Prep future TCP connection support (`connection.type: tcp`) alongside serial.
- MeshCore 2.x API: factory methods `create_serial`/`create_tcp`, subscription
  model, `get_contacts()` for contacts cache.
- Contacts cache refreshed every 30s; seeds user display names from `adv_name`.
- Advertisement and `NEW_CONTACT` event subscription for passive name tracking.
- `ADVERTISEMENT` payload only carries `public_key` — triggers contacts refresh
  to resolve `adv_name`. Contacts dict keyed by full 64-char public key;
  12-char `pubkey_prefix` derived as alias for inbound message matching.
- NWS API updated to `api.weather.gov/alerts/active/zone/{zone}`.
- Install/upgrade script: preserves config, creates `meshhall` service account,
  sets up venv at `/opt/meshhall/venv/`, deploys hardened systemd service.

### Plugin v0.1.0 — time, checkin, bulletin, frequencies, weather, replay
- Initial implementations of all core plugins.
- Weather: NWS forecast and alert polling, SDR/SAME alert ingestion via
  `tools/same_decoder.py`, configurable broadcast channel.
