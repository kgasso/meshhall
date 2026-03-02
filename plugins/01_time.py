"""Plugin: Time — !time returns current UTC and local time with timezone abbreviation."""

__version__ = "0.2.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Plugin version — update here when making changes to this plugin.
# PluginLoader reads __version__ to populate !version output.
from datetime import datetime, timezone
from core.database import PRIV_DEFAULT


def setup(dispatcher, config, db):

    async def cmd_time(msg):
        now_utc = datetime.now(timezone.utc)
        utc_str = now_utc.strftime("%Y-%m-%d %H:%Mz")
        try:
            # astimezone() uses the OS timezone; strftime %Z gives the
            # abbreviation (e.g. PST, PDT, MST) rather than "local".
            local_dt  = datetime.now().astimezone()
            local_str = local_dt.strftime("%H:%M %Z")
        except Exception:
            local_str = ""
        if local_str:
            return f"Time: {utc_str} / {local_str}"
        return f"Time: {utc_str}"

    dispatcher.register_command(
        "!time", cmd_time,
        help_text="Current UTC and local time",
        usage_text="!time",
        scope="channel",
        priv_floor=PRIV_DEFAULT,
        category="utility", plugin_name="time",
    )
