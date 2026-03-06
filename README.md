# MeshHall

A modular IRC-style bot for MeshCore mesh networks.
Runs on a Raspberry Pi connected to a RAK4631 WisBlock LoRa node via USB serial.

---

## Disclaimer
There is no warranty of fitness of this code for any purpose. It was originally
written to meet my needs of running on a Raspberry Pi 3B+, but likely will
work on other Linux or Unix-like operating systems.

This code is developed with both human authoring and agentic assistance.

This is pre-release code. Database changes should all be backward compatible,
but configuration changes may require manual intervention.

---

## Commands

Commands are sent as direct messages (DM) to the bot, or in a channel if marked
as channel-capable. Use `!help` for a live list filtered to your privilege level,
or `!help <command>` for detailed usage on any specific command.

> **Command character** — the default prefix is `!`. Operators can change it
> via `bot.command_char` in `config.yaml`. A space between the prefix and command
> name is tolerated (e.g. `! ping`) for mobile autocorrect compatibility.

---

### Core (built-in)

| Command | Scope | Description |
|---|---|---|
| `!ping` | channel | Check connectivity; returns hop count or `Direct` |
| `!about` | DM | About this bot and its operator contact |
| `!whoami` | DM | Your station name, node ID, and privilege level |
| `!help [cmd]` | channel/DM | Command list (filtered by privilege); `!help <cmd>` for details |
| `!version` | DM | Core version and loaded plugin versions |
| `!rehash` | DM | Reload all config files without restarting (admin) |
| `!restart` | DM | Restart the bot process — requires `!restart confirm` (admin) |
| `!shutdown` | DM | Shut down the bot — requires `!shutdown confirm` (admin) |

---

### Time (`01_time`)

| Command | Scope | Description |
|---|---|---|
| `!time` | channel | Current UTC and local time from the Pi system clock |

---

### Nets (`02_nets`)

Named check-in nets with recurring or ad-hoc sessions. Net control or admin
required for session and membership management. `!net` without a subcommand
prints the full subcommand list.

| Command | Scope | Description |
|---|---|---|
| `!net list` | DM/channel | List all active nets |
| `!net info <slug>` | DM/channel | Net details, schedule, and current status |
| `!net checkin [slug]` | DM/channel | Check in to a net (shortcut: `!checkin`) |
| `!net regrets <slug>` | DM | Register planned absence (shortcut: `!regrets`) |
| `!net roll [slug] [YYYY-MM-DD]` | DM/channel | Roll call for current or past session (shortcut: `!roll`) |
| `!net start <slug>` | DM/channel | Manually open a session (net control/admin); alias: `!open` |
| `!net stop <slug>` | DM/channel | Manually close a session (net control/admin); alias: `!close` |
| `!net schedule <slug> <schedule> [tz]` | DM | Set or clear recurrence (net control/admin) |
| `!net create <slug> <name> [opts]` | DM | Create a new net (net control/admin) |
| `!net delete <slug>` | DM | Deactivate a net (net control/admin) |
| `!net add <net> <user>` | DM | Add a member (net control/admin) |
| `!net remove <net> <user>` | DM | Remove a member (net control/admin) |
| `!net promote <net> <user>` | DM | Promote a guest to full member |
| `!net grant <net> <user>` | DM | Grant net control role (admin) |
| `!net revoke <net> <user>` | DM | Revoke net control role (admin) |

**`!net create` options:** `schedule "..."` `timezone TZ` `duration MIN` `channel CH` `guests yes|no` `description "..."`

**Schedule examples:** `weekly tuesday 19:00`, `monthly 3rd tuesday 19:00`, `daily 08:00`, `none` (ad-hoc)

**Shortcuts** (hidden from `!help` index):

| Shortcut | Equivalent |
|---|---|
| `!checkin [slug]` | `!net checkin [slug]` |
| `!regrets <slug>` | `!net regrets <slug>` |
| `!roll [slug] [date]` | `!net roll [slug] [date]` |
| `!open <slug>` | `!net start <slug>` |
| `!close <slug>` | `!net stop <slug>` |

---

### Bulletins (`03_bulletin`)

Requires privilege 2 (known member) to post. `!bulletin` without a subcommand
prints the full subcommand list.

| Command | Scope | Description |
|---|---|---|
| `!bulletin list [n]` | DM | List last N bulletins (default 5, max 20) |
| `!bulletin show <id>` | DM | Read a bulletin in full |
| `!bulletin post [msg]` | DM | Post inline, or publish pending draft if no text given |
| `!bulletin draft <text>` | DM | Start or append to a pending draft |
| `!bulletin draft clear` | DM | Discard pending draft |
| `!bulletin delete <id>` | DM | Delete a bulletin (own or any if admin) |

**Shortcuts** (hidden from `!help` index):

| Shortcut | Equivalent |
|---|---|
| `!post <msg>` | `!bulletin post <msg>` |
| `!bulletins [n]` | `!bulletin list [n]` |

---

### Frequencies (`04_frequencies`)

Frequency directory. `!freq` without a subcommand prints the full subcommand list.

| Command | Scope | Description |
|---|---|---|
| `!freq list [category]` | DM/channel | Browse the frequency directory |
| `!freq show <id>` | DM/channel | Look up a frequency by ID |
| `!freq add <name> <freq> <mode> <cat> [tone] [notes]` | DM | Add a frequency entry (admin) |
| `!freq delete <id>` | DM | Remove a frequency entry (admin) |

**Shortcuts** (hidden from `!help` index):

| Shortcut | Equivalent |
|---|---|
| `!freqs [category]` | `!freq list [category]` |

---

### Weather (`05_weather`)

NWS forecast and alert data. Requires internet access for live fetches; falls
back to cached data with a staleness note when NWS is unreachable.

Users can save a home ZIP with `!setloc` — bare `!wx` and `!wxalert` will then
use their personal location instead of the bot's configured area.

| Command | Scope | Description |
|---|---|---|
| `!wx` | DM/channel | Forecast for your `!setloc` ZIP, or the bot's configured area |
| `!wx <zip>` | DM/channel | NWS forecast for any US ZIP code |
| `!wx [periods]` | DM/channel | 1–8 forecast periods for home ZIP (default 2) |
| `!wx <zip> [periods]` | DM/channel | ZIP + period count |
| `!setloc <zip>` | DM | Save your home ZIP for personalised `!wx` and `!wxalert` |
| `!setloc` | DM | Show your current home ZIP |
| `!setloc clear` | DM | Remove your saved home ZIP |
| `!wxalert` | DM/channel | Active NWS/EAS alerts for your `!setloc` ZIP (or home zone) |
| `!wxalert list [zip]` | DM/channel | Active alerts for area or any US ZIP code |
| `!wxalert show <id>` | DM | Full text of a stored alert by ID |
| `!wxrefresh` | DM | Force an immediate NWS data refresh (admin) |

**Shortcuts** (hidden from `!help` index):

| Shortcut | Equivalent |
|---|---|
| `!alerts [zip]` | `!wxalert list [zip]` |

ZIP lookup requires `data/zip_code_database.csv` — see
[weather.yaml](config/plugins/weather.yaml) for source and column mapping.

> **Note:** `alerts.weather.gov` was decommissioned on December 2, 2025.
> MeshHall uses `api.weather.gov` which is the current NWS API endpoint.

---

### Replay (`06_replay`)

Message history. `!replay` without a subcommand defaults to list.

| Command | Scope | Description |
|---|---|---|
| `!replay [n \| Xh \| Xd]` | channel | Replay recent messages — last N, last X hours, or last X days |
| `!replay search <term>` | channel | Search message history by keyword |

**Shortcuts** (hidden from `!help` index):

| Shortcut | Equivalent |
|---|---|
| `!search <term>` | `!replay search <term>` |

---

### Users (`07_users`)

| Command | Scope | Description |
|---|---|---|
| `!whoami` | DM | Your station name, node ID, and privilege level (built-in) |
| `!whois <name\|ID>` | DM | Look up a user by display name or node ID |
| `!users [filter]` | DM | List known users with privilege levels (admin) |
| `!setpriv <id\|name> <0-15>` | DM | Set a user's privilege level (admin) |
| `!mute <id\|name>` | DM | Mute a user — sets privilege 0; cannot mute admins (admin) |
| `!unmute <id\|name>` | DM | Restore a muted user to default privilege (admin) |

**Privilege levels:**

| Level | Label | Description |
|---|---|---|
| 0 | muted | All messages silently dropped |
| 1 | default | Standard access, auto-assigned on first contact |
| 2–14 | configurable | Per-command floors set in plugin configs |
| 15 | admin | Full access |

Admins cannot mute or change the privilege of other admins directly. Use
`!setpriv <user> <n>` to reduce an admin below 15 first, then mute if needed.

---

### Channels (`08_channels`)

| Command | Scope | Description |
|---|---|---|
| `!channel list` | DM | List channel slots enumerated from the radio |
| `!channel set <idx> on\|off` | DM | Enable or disable bot responses on a slot (admin) |
| `!channel sync` | DM | Re-enumerate channels from the radio (admin) |

---

### MOTD (`09_motd`)

| Command | Scope | Description |
|---|---|---|
| `!motd` | DM | Show the current message of the day |
| `!setmotd <text>` | DM | Set the message of the day (admin) |
| `!clearmotd` | DM | Clear the message of the day (admin) |

The MOTD is delivered automatically on a user's first contact (or when the intro
window elapses), if one is set.

---

### Statistics (`10_stats`)

Admin-only, DM only.

| Command | Description |
|---|---|
| `!stats` | Full summary across all sections |
| `!stats messages` | Message volume (24h / 7d / all time) and top senders |
| `!stats users` | Active user counts (24h / 7d) and total known users |
| `!stats channels` | Most active channels by message count (7d) |
| `!stats commands` | Top commands by dispatch count this session |
| `!stats alerts` | NWS alerts stored (7d / active / all time) |
| `!stats uptime` | Bot process uptime and start timestamp |
| `!stats wx` | ZIP forecast cache hit rate since last restart |

---

## Requirements

### Hardware

- Raspberry Pi 3B+ or newer (3B+ tested; Pi 4 with SSD recommended for heavy use)
- RAK4631 WisBlock Core on RAK19007 Base Board
- 915 MHz LoRa antenna
- RECOMMENDED: DS3231 RTC module (accurate timekeeping without internet)
- RECOMMENDED: UPS HAT + LiPo/18650 cells

### Software
- Python 3.11+
- Python libraries — resolved by `install.sh` (see `requirements.txt`)

---

## Installation

### 1. Clone / copy files to the Pi

```bash
sudo apt-get install git
git clone https://github.com/kgasso/meshhall.git ~/meshhall-src
cd ~/meshhall-src
```

### 2. Run the installer (as root)

The installer creates a `meshhall` service account, installs to `/opt/meshhall`,
creates a Python venv, installs dependencies, and registers a systemd service.

```bash
sudo bash deploy/install.sh
```

### 3. Configure

All configuration lives in `/opt/meshhall/config/`.

**Main config** — connection type, bot admins, timezone:
```bash
sudo nano /opt/meshhall/config/config.yaml
```

**Plugin configs** — each plugin has its own file in `config/plugins/`:
```bash
sudo nano /opt/meshhall/config/plugins/weather.yaml      # home ZIP, NWS zone
sudo nano /opt/meshhall/config/plugins/frequencies.yaml  # seed frequency data
sudo nano /opt/meshhall/config/plugins/bulletin.yaml     # optional tuning
sudo nano /opt/meshhall/config/plugins/replay.yaml       # optional tuning
sudo nano /opt/meshhall/config/plugins/nets.yaml         # net scope/priv settings
```

**Set your NWS zone code** for weather alerts:

```bash
curl "https://api.weather.gov/points/42.4390,-123.3284" | python3 -m json.tool | grep forecastZone
# Look for the zone code at the end of the URL, e.g. "ORZ011"
```

### 4. Start the service

```bash
sudo systemctl start meshhall
sudo journalctl -u meshhall -f
```

---

## Connection Types

### Serial (default — recommended for RAK4631)

```yaml
# config/config.yaml
connection:
  type: serial
  serial_port: /dev/ttyACM0
  baud_rate: 115200
```

### TCP (ESP32-based devices only — untested)

> TCP connectivity has not been tested. Use serial for all current deployments.

```yaml
connection:
  type: tcp
  tcp_host: 192.168.1.100
  tcp_port: 5000
```

---

## Service Account Details

The installer creates a `meshhall` system account with no login shell, home at
`/opt/meshhall`, and membership in the `dialout` group for serial access.

```bash
sudo usermod -aG meshhall $USER   # edit config files without sudo
newgrp meshhall
```

---

## Python Virtual Environment

```bash
source /opt/meshhall/venv/bin/activate
pip install some-package              # then add to requirements.txt
deactivate

sudo systemctl stop meshhall
cd /opt/meshhall && /opt/meshhall/venv/bin/python meshhall.py   # run manually

sudo /opt/meshhall/venv/bin/pip install -r /opt/meshhall/requirements.txt
sudo systemctl restart meshhall
```

---

## Adding Plugins

1. Copy `plugins/_template.py` to `plugins/XX_myplugin.py`
2. Implement `setup(dispatcher, config, db)`
3. Load your plugin config with `cfg = config.plugin("myplugin")`
4. Create `config/plugins/myplugin.yaml` with your settings
5. Restart: `sudo systemctl restart meshhall`

Prefix filename with a two-digit number to control load order.
Prefix with underscore to disable without deleting: `_disabled_plugin.py`.

---

## Architecture

```
meshhall.py                  Entry point and event loop
core/
  config.py                  Main config + per-plugin config loader
  connection.py              MeshCore serial/TCP adapter + reply queue drain
  database.py                Async SQLite (aiosqlite) + plugin schema registry
  dispatcher.py              Command router, event bus, message chunker
  plugin_loader.py           Auto-discovers plugins/*.py
  ratelimit.py               Token bucket rate limiter for channel commands
plugins/
  01_time.py                 !time
  02_nets.py                 !net · subcommands: list info checkin regrets roll start stop schedule create delete add remove promote grant revoke
  03_bulletin.py             !bulletin · subcommands: list show post draft delete
  04_frequencies.py          !freq · subcommands: list show add delete
  05_weather.py              !wx  !wxalert  !setloc
  06_replay.py               !replay · subcommands: list search
  07_users.py                !whois  !users  !setpriv  !mute  !unmute
  08_channels.py             !channel · subcommands: list set sync
  09_motd.py                 !motd  !setmotd  !clearmotd
  10_stats.py                !stats
  _template.py               Copy this to create new plugins
config/
  config.yaml                Main settings (connection, bot identity, timezone, logging)
  plugins/
    bulletin.yaml            Bulletin plugin tuning
    frequencies.yaml         Frequency plugin tuning and seed data
    nets.yaml                Nets plugin scope/privilege settings
    replay.yaml              Replay plugin tuning
    time.yaml                Time plugin tuning
    weather.yaml             Weather plugin settings (zone, home ZIP, etc.)
deploy/
  install.sh                 Full installer (service account, venv, systemd)
  meshhall.service           systemd unit
data/                        Created at runtime (owned by meshhall user)
  meshhall.db                SQLite database (WAL mode)
  meshhall.log               Log file
  zip_code_database.csv      ZIP centroid data for !wx / !wxalert (operator-provided)
```

> **NOAA SAME/EAS offline alerts** — the RTL-SDR decoder has been moved to a
> separate repository: [meshhall-same](https://github.com/kgasso/meshhall-same).
> It feeds offline weather alerts into MeshHall's database with zero internet
> dependency — highly recommended if you have an RTL-SDR dongle.

---

## Grid-Down Notes

- Most commands work from cached/stored SQLite data — no internet required
- `!wx` and `!wxalert` fall back to cached data when NWS is unreachable
- [meshhall-same](https://github.com/kgasso/meshhall-same) + RTL-SDR provides weather alerts without internet
- DS3231 RTC keeps accurate timestamps when internet is unavailable
- UPS HAT + LiPo/18650 cells keep the Pi powered during outages
- WAL-mode SQLite is safe for concurrent and durable access
- SSD strongly preferred over SD card for frequent database writes
