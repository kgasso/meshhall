# MeshHall

A modular IRC-style bot for MeshCore mesh networks.
Runs on a Raspberry Pi connected to a RAK4631 WisBlock LoRa node via USB serial.

---

## Disclaimer
There is no warranty of fitness of this code for any purpose. It was originally
written to meet my needs of running on a Raspberry Pi 3B+, but likely will
work on other Linux or Unix-like operating systems. 

This code is developed with both human authoring and agentic assistance.

---

## Commands

| Command | Description |
|---|---|
| `!time` | Current UTC + local time from Pi system clock (RTC-backed) |
| `!checkin [note]` | Log a welfare check-in |
| `!status [callsign]` | Last check-in for a station |
| `!missing [hours]` | Stations not checked in within N hours (default 24) |
| `!roll` | All stations and last check-in time |
| `!post <msg>` | Post a bulletin |
| `!bulletins [n]` | List recent bulletins |
| `!bulletin <id>` | Read a bulletin in full |
| `!delbul <id>` | Delete a bulletin (own or admin) |
| `!freqs [category]` | Frequency directory |
| `!freq <n>` | Look up a specific frequency |
| `!addfreq …` | (Admin) Add a frequency entry |
| `!wx` | Cached NWS weather forecast |
| `!alerts` | Active NWS weather alerts |
| `!alert <id>` | Read full alert text |
| `!replay [n\|Xh\|Xd]` | Replay message history |
| `!search <term>` | Search message history |
| `!help` | List all commands |

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
sudo nano /opt/meshhall/config/plugins/checkin.yaml      # optional tuning
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
  02_checkin.py             !checkin !status !missing !roll
  03_bulletin.py            !post !bulletins !bulletin !delbul
  04_frequencies.py         !freqs !freq !addfreq !delfreq
  05_weather.py             !wx !alerts !alert
  06_replay.py              !replay !search
  07_users.py               !whois !users !setpriv !mute !unmute
  08_channels.py            !channels !channel
  _template.py              Copy this to create new plugins
config/
  config.yaml               Main settings (connection, bot identity, logging)
  plugins/
    bulletin.yaml           Bulletin plugin tuning
    checkin.yaml            Check-in plugin tuning
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
