"""
Plugin: Node
Commands:
  !node              -- alias for !node info
  !node info         -- radio config and node identity
  !node hw           -- hardware health (battery, flash, uptime, errors)
  !node rf           -- RF conditions and packet statistics

All subcommands are DM only, admin only.
Data is fetched on-demand from the radio via dispatcher.get_node_info() --
never cached, so values always reflect the current state of the radio,
including any changes made via the radio UI or future !node set commands.

The same dispatcher.get_node_info() is available to other plugins (e.g.
the meshhall-web API plugin) without making direct serial calls.

Config: none (no config/plugins/node.yaml needed)
"""

__version__ = "0.3.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import time

from core.database import PRIV_ADMIN

CONFIRM_TTL = 60  # seconds -- matches dispatcher !restart / !shutdown


# -- AutoAdd config bitmask decoder -------------------------------------------
# Confirmed from live firmware data (config=3 with only Chat Users checked):
#   bit0 = Selected mode active (0 = Auto Add All, 1 = Auto Add Selected)
#   bit1 = Chat Users enabled
#   bit2 = Repeaters enabled
#   bit3 = Room Servers enabled
#   bit4 = Sensors enabled
# config=3 (0b00011) = Selected mode + Chat Users -> matches firmware screenshot.
_AUTOADD_BITS = [
    (1, "Chat"),
    (2, "Repeaters"),
    (3, "Rooms"),
    (4, "Sensors"),
]

def _decode_autoadd(config_val) -> str:
    if config_val is None:
        return "unknown"
    try:
        val = int(config_val)
    except (TypeError, ValueError):
        return str(config_val)
    # bit0 = mode: 0=Auto Add All, 1=Selected types only
    mode = "Selected" if (val & 1) else "All"
    enabled = [label for bit, label in _AUTOADD_BITS if val & (1 << bit)]
    if mode == "All":
        return "All contacts"
    return f"Selected: {', '.join(enabled)}" if enabled else "Selected: none"


def _fmt_uptime(seconds) -> str:
    if seconds is None:
        return "unknown"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _get(payload, *keys, default="n/a"):
    """Safely extract a value from a dict, returning default if missing/None."""
    if not isinstance(payload, dict):
        return default
    for key in keys:
        val = payload.get(key)
        if val is not None:
            return val
    return default


# -- Section builders ---------------------------------------------------------

def _section_info(info: dict) -> str:
    """Radio config and node identity (send_appstart payload)."""
    si = info.get("self_info") or {}
    ac = info.get("autoadd") or {}

    name    = _get(si, "name")
    pubkey  = _get(si, "public_key", default="")
    prefix  = pubkey[:12] + "..." if len(pubkey) >= 12 else pubkey or "n/a"
    freq    = _get(si, "radio_freq")
    bw      = _get(si, "radio_bw")
    sf      = _get(si, "radio_sf")
    cr      = _get(si, "radio_cr")
    txpow   = _get(si, "tx_power")
    maxtx   = _get(si, "max_tx_power")
    autoadd = _decode_autoadd(_get(ac, "config", default=None))

    freq_str = f"{freq} MHz" if freq != "n/a" else "n/a"
    bw_str   = f"{bw} kHz"  if bw   != "n/a" else "n/a"
    tx_str   = f"{txpow} dBm (max {maxtx})" if txpow != "n/a" else "n/a"

    lines = [
        f"Node: {name}",
        f"Key:  {prefix}",
        f"Freq: {freq_str}  BW: {bw_str}",
        f"SF:   {sf}  CR: {cr}  TX: {tx_str}",
        f"AutoAdd: {autoadd}",
    ]
    return "\n".join(lines)


def _section_hw(info: dict) -> str:
    """Hardware health: battery, flash, uptime, error count."""
    bat  = info.get("battery")   or {}
    tele = info.get("telemetry") or {}
    core = info.get("stats_core") or {}

    # Battery voltage -- prefer telemetry (more precise), fall back to stats_core mV
    voltage = None
    lpp = tele.get("lpp") if isinstance(tele, dict) else None
    if isinstance(lpp, list):
        for entry in lpp:
            if isinstance(entry, dict) and entry.get("type") == "voltage":
                voltage = entry.get("value")
                break
    if voltage is None:
        mv = _get(core, "battery_mv", default=None)
        if mv is not None:
            try:
                voltage = round(int(mv) / 1000, 2)
            except (TypeError, ValueError):
                pass

    volt_str   = f"{voltage:.2f}V" if voltage is not None else "n/a"
    used_kb    = _get(bat, "used_kb")
    total_kb   = _get(bat, "total_kb")
    flash_str  = f"{used_kb} / {total_kb} KB" if used_kb != "n/a" else "n/a"
    uptime     = _fmt_uptime(_get(core, "uptime_secs", default=None))
    errors     = _get(core, "errors", default=0)
    queue      = _get(core, "queue_len", default=0)

    lines = [
        f"Battery: {volt_str}",
        f"Flash:   {flash_str}",
        f"Uptime:  {uptime}",
        f"Errors:  {errors}  Queue: {queue}",
    ]
    return "\n".join(lines)


def _section_rf(info: dict) -> str:
    """RF conditions and firmware packet statistics."""
    radio   = info.get("stats_radio")   or {}
    packets = info.get("stats_packets") or {}

    noise    = _get(radio, "noise_floor")
    rssi     = _get(radio, "last_rssi")
    snr      = _get(radio, "last_snr")
    tx_air   = _get(radio, "tx_air_secs")
    rx_air   = _get(radio, "rx_air_secs")

    recv     = _get(packets, "recv")
    sent     = _get(packets, "sent")
    flood_tx = _get(packets, "flood_tx")
    dir_tx   = _get(packets, "direct_tx")
    flood_rx = _get(packets, "flood_rx")
    dir_rx   = _get(packets, "direct_rx")
    pkt_err  = _get(packets, "recv_errors")

    noise_str = f"{noise} dBm" if noise != "n/a" else "n/a"
    rssi_str  = f"{rssi} dBm" if rssi  != "n/a" else "n/a"
    snr_str   = f"{snr} dB"   if snr   != "n/a" else "n/a"

    lines = [
        f"Noise: {noise_str}  RSSI: {rssi_str}  SNR: {snr_str}",
        f"Air:   TX {tx_air}s  RX {rx_air}s",
        f"Recv:  {recv} ({flood_rx} flood / {dir_rx} direct)  Errors: {pkt_err}",
        f"Sent:  {sent} ({flood_tx} flood / {dir_tx} direct)",
    ]
    return "\n".join(lines)


# -- !node set helpers --------------------------------------------------------

# Fields requiring confirmation (disruptive radio changes)
_CONFIRM_REQUIRED = {"freq", "name", "txpower"}

# autoadd token -> bitmask bit index
_AUTOADD_TOKENS = {
    "chat":     1,
    "repeater": 2,
    "repeaters":2,
    "room":     3,
    "rooms":    3,
    "sensor":   4,
    "sensors":  4,
}


def _build_autoadd_mask(tokens: list) -> int:
    """
    Build an autoadd config bitmask from a list of type tokens.
    Always sets bit0 (Selected mode). Unknown tokens are ignored.
    Returns -1 if no valid tokens provided.
    """
    mask = 1  # bit0 = Selected mode
    matched = False
    for t in tokens:
        bit = _AUTOADD_TOKENS.get(t.lower().strip(","))
        if bit is not None:
            mask |= (1 << bit)
            matched = True
    return mask if matched else -1


async def _do_set(dispatcher, sender_id, field, args) -> str:
    """Execute a confirmed (or confirmation-exempt) !node set action."""
    try:
        if field == "freq":
            try:
                freq = float(args[0])
            except (ValueError, IndexError):
                return "Usage: !node set freq <MHz>  e.g. 910.525"
            # set_radio requires all current radio params; fetch first
            info = await dispatcher.get_node_info()
            si = (info.get("self_info") or {})
            bw = si.get("radio_bw", 62.5)
            sf = si.get("radio_sf", 7)
            cr = si.get("radio_cr", 5)
            # set_radio signature: (freq, bw, sf, cr)
            conn = dispatcher._node_info_provider.__self__
            await conn._mc.commands.set_radio(freq, bw, sf, cr)
            return f"Frequency set to {freq} MHz."

        elif field == "name":
            if not args:
                return "Usage: !node set name <name>"
            name = " ".join(args)
            conn = dispatcher._node_info_provider.__self__
            await conn._mc.commands.set_name(name)
            return f"Node name set to '{name}'."

        elif field == "txpower":
            try:
                pwr = int(args[0])
            except (ValueError, IndexError):
                return "Usage: !node set txpower <dBm>"
            conn = dispatcher._node_info_provider.__self__
            await conn._mc.commands.set_tx_power(pwr)
            return f"TX power set to {pwr} dBm."

        elif field == "autoadd":
            if not args:
                return (
                    "Usage: !node set autoadd <types>\n"
                    "Types: chat, repeater, room, sensor (comma or space separated)\n"
                    "Example: !node set autoadd chat"
                )
            mask = _build_autoadd_mask(args)
            if mask == -1:
                return (
                    f"No valid types recognised. "
                    f"Use: chat, repeater, room, sensor"
                )
            conn = dispatcher._node_info_provider.__self__
            await conn._mc.commands.set_autoadd_config(mask)
            # Decode for confirmation message
            info = await dispatcher.get_node_info()
            ac = (info.get("autoadd") or {})
            decoded = _decode_autoadd(ac.get("config"))
            return f"AutoAdd updated. Current: {decoded}"

        else:
            return f"Unknown field '{field}'."

    except Exception as e:
        return f"Error applying !node set {field}: {e}"


# -- Plugin setup -------------------------------------------------------------

def setup(dispatcher, config, db):

    async def cmd_node(msg):
        raw  = msg.arg_str.strip()
        parts = raw.split()
        sub  = parts[0].lower() if parts else ""

        cc = dispatcher.command_char

        # ── No subcommand -> usage ────────────────────────────────────────────
        if sub == "":
            return (
                f"Node commands:\n"
                f"  {cc}node info              -- radio config and identity\n"
                f"  {cc}node hw                -- battery, flash, uptime\n"
                f"  {cc}node rf                -- RF conditions and packet stats\n"
                f"  {cc}node set [field] [val] -- configure a field; omit args to list options"
            )

        # ── Read subcommands ──────────────────────────────────────────────────
        if sub in ("info", "hw", "rf"):
            try:
                info = await dispatcher.get_node_info()
            except Exception as e:
                return f"Error querying node: {e}"
            if sub == "info":
                return _section_info(info)
            elif sub == "hw":
                return _section_hw(info)
            elif sub == "rf":
                return _section_rf(info)

        # ── !node set ─────────────────────────────────────────────────────────
        if sub == "set":
            field = parts[1].lower() if len(parts) > 1 else ""
            args  = parts[2:] if len(parts) > 2 else []

            if not field:
                return (
                    f"Settable fields:\n"
                    f"  {cc}node set freq <MHz>        -- radio frequency\n"
                    f"  {cc}node set name <n>        -- node display name\n"
                    f"  {cc}node set txpower <dBm>     -- transmit power\n"
                    f"  {cc}node set autoadd <types>   -- auto-add contact types\n"
                    f"\n"
                    f"freq, name, txpower require: {cc}node set confirm\n"
                    f"To cancel a pending change: {cc}cancel"
                )

            all_fields = {"freq", "name", "txpower", "autoadd"}
            if field not in all_fields and field not in ("confirm", "cancel"):
                return (
                    f"Unknown field '{field}'. "
                    f"Use {cc}node set for the list."
                )

            sid = msg.sender_id
            who = await db.format_user(msg.sender_id, msg.sender_name)

            # ── Cancel step ───────────────────────────────────────────────────
            if field == "cancel":
                pending = dispatcher._pending_confirm.get(sid)
                if not pending:
                    return f"No pending {cc}node set to cancel."
                action_name = pending.get("action", "unknown")
                del dispatcher._pending_confirm[sid]
                dispatcher.log_admin_attempt(
                    "!node set cancel", msg, granted=True,
                    reason=f"cancelled pending {action_name}"
                )
                return f"Cancelled pending {action_name}."

            # ── Confirm step ──────────────────────────────────────────────────
            if field == "confirm":
                pending = dispatcher._pending_confirm.get(sid)
                if not pending or not pending.get("action", "").startswith("node_set_"):
                    return f"No pending {cc}node set to confirm."
                if time.time() > pending["expires"]:
                    del dispatcher._pending_confirm[sid]
                    dispatcher.log_admin_attempt(
                        "!node set confirm", msg, granted=False,
                        reason="confirmation window expired"
                    )
                    return (
                        f"Confirmation window expired. "
                        f"Send {cc}node set {pending['field']} {' '.join(pending['args'])} again to start over."
                    )
                # Confirmed -- clear pending, log, execute
                del dispatcher._pending_confirm[sid]
                dispatcher.log_admin_attempt(
                    f"!node set {pending['field']}", msg, granted=True,
                    reason=f"confirmed; value={' '.join(pending['args'])}"
                )
                return await _do_set(
                    dispatcher, sid,
                    pending["field"], pending["args"]
                )

            # ── autoadd -- no confirm required ────────────────────────────────
            if field not in _CONFIRM_REQUIRED:
                dispatcher.log_admin_attempt(
                    f"!node set {field}", msg, granted=True,
                    reason=f"value={' '.join(args)}"
                )
                return await _do_set(dispatcher, sid, field, args)

            # ── Disruptive fields -- require confirm ──────────────────────────
            if not args:
                return f"Usage: {cc}node set {field} <value>"

            dispatcher._pending_confirm[sid] = {
                "action":  f"node_set_{field}",
                "field":   field,
                "args":    args,
                "expires": time.time() + CONFIRM_TTL,
            }
            val_preview = " ".join(args)
            dispatcher.log_admin_attempt(
                f"!node set {field}", msg, granted=False,
                reason=f"awaiting confirmation; value={val_preview}"
            )
            return (
                f"About to set {field} to: {val_preview}\n"
                f"This may affect radio connectivity. "
                f"Send '{cc}node set confirm' to apply, or '{cc}cancel' to abort.\n"
                f"(confirmation expires in {CONFIRM_TTL}s)"
            )

        # ── Unknown subcommand ────────────────────────────────────────────────
        return (
            f"Unknown subcommand '{sub}'. "
            f"Use {cc}node for the command list."
        )

    dispatcher.register_admin_command(
        "!node", cmd_node,
        help_text="Radio node status and configuration",
        usage_text=(
            "!node                     -- show subcommand list\n"
            "!node info                -- radio config and identity\n"
            "!node hw                  -- battery, flash, uptime, errors\n"
            "!node rf                  -- RF conditions and packet stats\n"
            "!node set                 -- list settable fields\n"
            "!node set freq <MHz>      -- set radio frequency (confirm required)\n"
            "!node set name <n>      -- set node display name (confirm required)\n"
            "!node set txpower <dBm>   -- set TX power (confirm required)\n"
            "!node set autoadd <types> -- set auto-add contact types\n"
            "!node set confirm         -- confirm a pending disruptive change\n"
            "!cancel                   -- cancel any pending confirmation"
        ),
        scope="direct", priv_floor=PRIV_ADMIN,
        category="admin", plugin_name="node",
    )
