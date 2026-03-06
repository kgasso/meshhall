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
| `!version` | DM | Core version and loaded plugin versions (admin) |
| `!rehash` | DM | Reload all config files without restarting (admin) |
| `!restart` | DM | Restart the bot process — requires `!restart confirm` (admin) |
| `!shutdown` | DM | Shut down the bot — requires `!shutdown confirm` (admin) |

---

### Time (`01_time`)

| Command | Scope | Description |
|---|---|---|
| `!time` | channel | Current UTC and local time from the Pi system clock |

---


---

### Bulletins (`03_bulletin`)

Requires privilege 2 (known member) to post or delete.

| Command | Scope | Description |
|---|---|---|
| `!bulletins [n]` | DM | List recent bulletins |
| `!bulletin <id>` | DM | Read a bulletin in full |
| `!post <message>` | DM | Post a new bulletin |
| `!delbul <id>` | DM | Delete a bulletin (own bulletins, or any if admin) |

---

### Frequencies (`04_frequencies`)

| Command | Scope | Description |
|---|---|---|
| `!freqs [category]` | DM | Browse the frequency directory |
| `!freq <n>` | DM | Look up a frequency by number |
| `!addfreq NAME FREQ MODE CAT [TONE] [notes]` | DM | Add a frequency entry (admin) |
| `!delfreq <n>` | DM | Remove a frequency entry (admin) |

---

### Weather (`05_weather`)

NWS forecast and alert data. All weather commands are DM-only. Requires internet
access for live fetches; falls back to cached data with a timestamp note when
NWS is unreachable.

Users can save a home ZIP with `!setloc` — bare `!wx` and `!alerts` will then
use their personal location instead of the bot's configured area.

| Command | Description |
|---|---|
| `!wx` | Forecast for your `!setloc` ZIP, or the bot's configured area if not set |
| `!wx <zip>` | NWS forecast for any US ZIP code |
| `!wx [periods]` | 1–8 forecast periods for the local area (default 2) |
| `!setloc <zip>` | Save your home ZIP for personalised `!wx` and `!alerts` |
| `!setloc` | Show your current home ZIP |
| `!setloc clear` | Remove your saved home ZIP |
| `!alerts` | Active NWS/EAS alerts for your `!setloc` ZIP, or the home zone |
| `!alerts <zip>` | Active NWS alerts for any US ZIP code (live fetch) |
| `!alert <id>` | Read the full text of a stored alert by ID |
| `!wxrefresh` | Force an immediate NWS data refresh (admin) |

ZIP lookup requires `data/zip_code_database.csv` — see
[weather.yaml](config/plugins/weather.yaml) for source and column mapping.

---

### Statistics (`10_stats`)

Admin-only, DM only. All sections available individually or as a combined summary.

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

| Command | Description |
|---|---|
| `!motd` | Show the current message of the day |
| `!setmotd <text>` | Set the message of the day (admin) |
| `!clearmotd` | Clear the message of the day (admin) |

The MOTD is also delivered automatically after the welcome message on a user's
first contact (or when the intro window elapses), if one is set.

---

### Replay (`06_replay`)

| Command | Scope | Description |
|---|---|---|
| `!replay [n \| Xh \| Xd]` | channel | Replay recent messages — last N messages, last X hours, or last X days |
| `!search <term>` | channel | Search message history by keyword |

---

### Users (`07_users`)

| Command | Scope | Description |
|---|---|---|
| `!whois <name\|ID>` | DM | Look up a user by display name or node ID |
| `!users [filter]` | DM | List known users, optionally filtered |
| `!setpriv <id\|name> <0-15>` | DM | Set a user's privilege level (admin) |
| `!mute <id\|name>` | DM | Mute a user — sets privilege 0 (admin) |
| `!unmute <id\|name>` | DM | Restore a muted user to default privilege (admin) |

---

### Channels (`08_channels`)

| Command | Scope | Description |
|---|---|---|
| `!channels` | DM | List channel slots enumerated from the radio |
| `!channel <idx> on\|off` | DM | Enable or disable bot responses on a channel slot (admin) |
| `!channel sync` | DM | Re-enumerate channels from the radio (admin) |

---

## Requirements

### Hardware

- Raspberry Pi 4 (2GB+) with SSD recommended (if frequent database writes)
- RAK4631 WisBlock Core on RAK19007 Base Board
- 915 MHz LoRa antenna
- RECOMMENDED: DS3231 based RTC (accurate timekeeping without internet)
- RECOMMENDED: UPS / UPS HAT + LiPo/18650 cells

### Software
- Python 3
- Various Python libraries (install.sh will resolve, see requirements.txt)

---

## Installation

### 1. Clone / copy files to the Pi

```bash
# If you don't have Git installed, use sudo to install it locally
sudo apt-get install git

# As your regular user (e.g. 'pi')
git clone https://github.com/kgasso/meshhall.git ~/meshhall-src
cd ~/meshhall-src
```

### 2. Run the installer (as root)

The installer creates a `meshhall` service account, installs to `/opt/meshhall`,
creates a Python venv, installs dependencies, and registers systemd services.

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
sudo nano /opt/meshhall/config/plugins/weather.yaml      # zone, lat/lon, alert channel
sudo nano /opt/meshhall/config/plugins/frequencies.yaml  # seed frequency data
sudo nano /opt/meshhall/config/plugins/bulletin.yaml     # optional tuning
sudo nano /opt/meshhall/config/plugins/replay.yaml       # optional tuning
```

Plugin config files are **only read by the plugin that uses them**. Having a config
file present for a plugin that isn't loaded causes no errors.

**Set your NWS zone code** for weather alerts:

```bash
# Replace with your actual coordinates
curl "https://api.weather.gov/points/42.4390,-123.3284" | python3 -m json.tool | grep forecastZone
# Look for the zone code at the end of the forecastZone URL, e.g. "ORZ011"
```

Put that code in `config/plugins/weather.yaml` under `zone:`.

> **Note:** `alerts.weather.gov` was decommissioned on December 2, 2025.
> MeshHall uses `api.weather.gov` which is the current NWS API endpoint.

### 5. Start the service

```bash
sudo systemctl start meshhall

# Watch logs
sudo journalctl -u meshhall -f
```

---

## Connection Types

### Serial (default — recommended for RAK4631)

The RAK4631 is nRF52840-based and communicates over USB serial. This is the
simplest and most reliable connection method.

```yaml
# config/config.yaml
connection:
  type: serial
  serial_port: /dev/ttyACM0   # verify with: ls /dev/ttyACM*
  baud_rate: 115200
```

The `meshhall` service account is added to the `dialout` group by the installer,
which grants access to `/dev/ttyACM*` without needing root.

### TCP (ESP32-based devices only — untested, reserved for future implementation)

> **Note:** TCP connectivity has not been tested and likely does not work in
> the current release. It is reserved for future implementation once compatible
> hardware is available for testing. Use serial for all current deployments!

TCP connectivity is available on ESP32-based MeshCore companion builds (e.g.
Heltec V3, Station G2 — **not** the RAK4631, which is nRF52840).

The firmware must be compiled with `WIFI_SSID` and `WIFI_PWD` defined. The node
creates a TCP server on port 5000. Only **one TCP client can connect at a time** —
the bot holds that connection permanently, so you cannot simultaneously use the
companion app over WiFi while the bot is running.

```yaml
# config/config.yaml
connection:
  type: tcp
  tcp_host: 192.168.1.100   # LAN IP of the ESP32 node
  tcp_port: 5000             # default MeshCore TCP port
```

---

## Service Account Details

The installer creates a `meshhall` system account with:
- No login shell (`/usr/sbin/nologin`)
- No home directory in `/home`
- Home set to `/opt/meshhall`
- Member of `dialout` (serial/USB access)

To edit config files as your regular user without sudo, add yourself to the
`meshhall` group:

```bash
sudo usermod -aG meshhall $USER
newgrp meshhall   # or log out and back in
```

Config files in `/opt/meshhall/config/` are group-writable by the `meshhall` group.

---

## Python Virtual Environment

The venv lives at `/opt/meshhall/venv/`. To interact with it manually:

```bash
# Activate
source /opt/meshhall/venv/bin/activate

# Install a new package (then add it to requirements.txt)
pip install some-package

# Run the bot manually (stop the service first)
sudo systemctl stop meshhall
cd /opt/meshhall
/opt/meshhall/venv/bin/python meshhall.py

# Deactivate
deactivate
```

After updating `requirements.txt`, reinstall into the venv:
```bash
sudo /opt/meshhall/venv/bin/pip install -r /opt/meshhall/requirements.txt
sudo systemctl restart meshhall
```

---

## Adding Plugins

1. Copy `plugins/_template.py` to `plugins/XX_myplugin.py`
2. Implement `setup(dispatcher, config, db)`
3. Load your plugin config with `cfg = config.plugin("myplugin")`
   (reads `config/plugins/myplugin.yaml` if it exists — safe if absent)
4. Create `config/plugins/myplugin.yaml` with your settings
5. Restart the bot: `sudo systemctl restart meshhall`

Prefix the filename with a two-digit number to control load order.
Prefix with underscore to disable without deleting: `_disabled_plugin.py`.


---

## Architecture

```
meshhall.py                  Entry point and event loop
core/
  config.py                 Main config + per-plugin config loader
  connection.py             MeshCore serial/TCP adapter + reply queue drain
  database.py               Async SQLite (aiosqlite) + plugin schema registry
  dispatcher.py             Command router, event bus, message chunker
  plugin_loader.py          Auto-discovers plugins/*.py
  ratelimit.py              Token bucket rate limiter for channel commands
plugins/
  01_time.py                !time
  02_nets.py                !checkin !regrets !roll !nets !netinfo !mknet !rmnet !addmember !delmember !promote !ncgrant !ncrevoke
  03_bulletin.py            !post !bulletins !bulletin !delbul
  04_frequencies.py         !freqs !freq !addfreq !delfreq
  05_weather.py             !wx !alerts !alert
  06_replay.py              !replay !search
  07_users.py               !whois !users !setpriv !mute !unmute
  08_channels.py            !channels !channel
  09_motd.py                !motd !setmotd !clearmotd
  10_stats.py               !stats
  _template.py              Copy this to create new plugins
config/
  config.yaml               Main settings (connection, bot identity, logging)
  plugins/
    bulletin.yaml           Bulletin plugin tuning
    frequencies.yaml        Frequency plugin tuning and seed data
    replay.yaml             Replay plugin tuning
    time.yaml               Time plugin tuning
    weather.yaml            Weather plugin settings (zone, lat/lon, etc.)
deploy/
  install.sh                Full installer (service account, venv, systemd)
  meshhall.service           systemd unit
data/                       Created at runtime (owned by meshhall user)
  meshhall.db               SQLite database (WAL mode)
  meshhall.log              Log file
  zip_code_database.csv     ZIP centroid data for !wx <zip> (operator-provided)
```

> **NOAA SAME/EAS offline alerts** (RTL-SDR decoder) has been moved to a
> separate repository: [meshhall-same](https://github.com/kgasso/meshhall-same). The standalone SDR decoder feeds
> offline weather alerts into MeshHall's database with zero internet dependency
> — highly recommended if you have an RTL-SDR dongle.

---

## Grid-Down Notes

If you're architecting this to work in a grid-down scenario, there are several
recommendations to help keep services up and functional:

- Most commands work from cached/stored SQLite data — no internet required
- Weather API polling is internet-dependant (`!wx`, `!alerts`)
- Leveraging meshhall-same and an RTL-SDR will get you weather alerts without
  internet access if an NWS station is available
- DS3231 based RTC on a pi can keep accurate time (critical for timestamps)
- UPS / UPS HAT + LiPo/18650 cells can keep the Pi powered and bot functional
- WAL-mode SQLite should be safe for concurrent and durable access
- Usage of SSD strongly preferred over SD card for frequent database writes
