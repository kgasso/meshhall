#!/usr/bin/env python3
"""
MeshHall - A modular IRC-style bot for MeshCore mesh networks.
Entry point and main event loop.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Core version is defined in core/__init__.py -- edit there, not here.

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from core import __version__ as CORE_VERSION
from core.config import Config
from core.database import Database
from core.connection import ConnectionManager
from core.dispatcher import Dispatcher, PRIV_ADMIN
from core.plugin_loader import PluginLoader

logger = logging.getLogger(__name__)


def _check_permissions():
    """
    Warn at startup if config or data files have world-readable or
    world-writable permission bits set. Non-fatal -- the bot still starts,
    but the operator will see the warning immediately in journalctl.

    Checks world bits (o+r and o+w) on:
      - config/ directory and all *.yaml files inside it
      - data/ directory (DB and logs live here)
    """
    import stat as _stat

    targets = [Path("config"), Path("data")]
    for p in Path("config").rglob("*.yaml"):
        targets.append(p)

    for path in targets:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue  # data/ may not exist yet on first run

        world_read  = bool(mode & _stat.S_IROTH)
        world_write = bool(mode & _stat.S_IWOTH)
        world_exec  = bool(mode & _stat.S_IXOTH)

        if world_write:
            logger.error(
                f"SECURITY: {path} is world-writable (mode {oct(mode & 0o777)}) -- "
                "this allows any local user to modify bot config. "
                "Fix with: chmod o-rwx " + str(path)
            )
        elif world_read or world_exec:
            logger.warning(
                f"SECURITY: {path} is world-readable (mode {oct(mode & 0o777)}) -- "
                "config may contain sensitive values (admin IDs, coordinates). "
                "Fix with: chmod o-rwx " + str(path)
            )


async def main():
    # -- Bootstrap ------------------------------------------------------------
    config = Config("config/config.yaml")
    logging.basicConfig(
        level=getattr(logging, config.get("log_level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            # stdout is captured by journald via StandardOutput=journal in the
            # systemd unit. journald handles rotation automatically -- no flat
            # file handler needed. Use: journalctl -u meshhall -f
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger.info(f"MeshHall v{CORE_VERSION} starting up...")
    _check_permissions()

    # -- Dependency version check ----------------------------------------------
    try:
        from importlib.metadata import version as pkg_version
        from packaging.version import Version
        mc_version = pkg_version("meshcore")
        if Version(mc_version) < Version("2.1.0"):
            logger.error(
                f"meshcore {mc_version} is too old -- 2.1.0+ required. "
                "Run: pip install --upgrade meshcore"
            )
            sys.exit(1)
        logger.info(f"meshcore version: {mc_version}")
    except Exception as e:
        logger.warning(f"Could not verify meshcore version: {e}")

    db = Database(config.get("db_path", "data/meshhall.db"))

    dispatcher = Dispatcher(config, db)

    # Plugins must load BEFORE db.initialize() so their db.register_schema()
    # calls are collected first. initialize() then creates all tables in one pass.
    loader = PluginLoader(dispatcher, config, db)
    loader.load_all("plugins")

    # Load config-defined aliases now that all plugins are registered.
    dispatcher.load_config_aliases()

    # Register !version command now that loader is populated
    _register_version_cmd(dispatcher, loader)

    # ConnectionManager also registers a schema (_dedup, _channels) -- same requirement.
    conn = ConnectionManager(config, dispatcher, db)

    # Give the channels plugin a reference to conn so it can call
    # enumerate_channels() from !channel sync and the rehash callback.
    # The plugin registered a placeholder setup() with conn=None; we inject
    # it here by calling the module's _inject_conn() if it exists.
    _inject_conn_to_plugins(loader, conn)
    _register_advertise_cmd(dispatcher, conn)
    _register_prune_cmd(dispatcher, conn)
    # !contacts deprecated -- use !status (coming in next release)

    # Now initialize -- core schema + all plugin schemas created here.
    await db.initialize()
    logger.info("Database initialized.")

    # Bootstrap admin privileges
    admin_ids = config.get("bot.admins", [])
    for admin_id in admin_ids:
        await db.upsert_user(admin_id)  # return value intentionally ignored
        await db.set_privilege(admin_id, 15)
        logger.info(f"Admin bootstrap: {admin_id} set to privilege 15")

    # -- Graceful shutdown + SIGHUP rehash ------------------------------------
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(conn, db)))

    def _sighup_handler():
        logger.info("SIGHUP received -- triggering rehash.")
        asyncio.create_task(dispatcher.do_rehash())

    loop.add_signal_handler(signal.SIGHUP, _sighup_handler)

    # Register the system action callback for !restart / !shutdown.
    #
    # Restart: exit with code 0 -- systemd (Restart=always) will restart the bot.
    # Shutdown: exit with code 42 -- listed in RestartPreventExitCodes so systemd
    #   treats it as an intentional stop and does NOT restart.
    async def system_action(action: str):
        if action == "restart":
            logger.warning("ADMIN: Restarting via clean exit (systemd will restart)...")
            await conn.stop()
            await db.close()
            sys.exit(0)
        elif action == "shutdown":
            logger.warning("ADMIN: Shutting down cleanly (exit 42 -- no restart)...")
            await conn.stop()
            await db.close()
            sys.exit(42)

    # Note: dispatcher providers (contact_count, node_info) are registered
    # automatically by ConnectionManager when the radio connects -- no wiring
    # needed here. See core/connection.py _connect().

    dispatcher.set_system_action_callback(system_action)

    await conn.run()


def _inject_conn_to_plugins(loader: PluginLoader, conn):
    """
    After ConnectionManager is created, give any plugin that has an
    _inject_conn() module-level function a reference to it. This lets
    plugins like 08_channels call conn.enumerate_channels() without
    requiring conn to exist at plugin load time (which would create a
    circular dependency -- conn needs db, db needs schemas from plugins).
    """
    import sys
    for stem in loader.loaded_plugins:
        mod_name = f"plugins.{stem}"
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "_inject_conn"):
            try:
                mod._inject_conn(conn)
                logger.debug(f"Injected conn into plugin '{stem}'")
            except Exception as e:
                logger.warning(f"_inject_conn failed for plugin '{stem}': {e}")


def _register_version_cmd(dispatcher: Dispatcher, loader: PluginLoader):
    """Register !version as a built-in after the loader has finished."""

    async def cmd_version(msg):
        lines = [f"MeshHall core v{CORE_VERSION}"]
        plugins = loader.loaded_plugins
        if not plugins:
            lines.append("No plugins loaded.")
        else:
            lines.append(f"Plugins ({len(plugins)}):")
            for stem, (ver, _path) in sorted(plugins.items()):
                display = stem.lstrip("0123456789_")
                lines.append(f"  {display}: v{ver}")
        return "\n".join(lines)

    dispatcher.register_command(
        "!version", cmd_version,
        help_text="Show core and plugin version info",
        scope="direct",
        priv_floor=PRIV_ADMIN,
        is_admin=False,
        category="core",
    )


def _register_advertise_cmd(dispatcher: Dispatcher, conn):
    """Register !advertise as an admin-only on-demand advertisement trigger."""

    async def cmd_advertise(msg):
        if not conn._adv_method:
            return (
                "No advertisement method available -- check meshcore version. "
                "See startup logs for details."
            )
        ok = await conn.send_advertisement()
        if ok:
            interval = conn.config.get("bot.advertise_interval", 0)
            note = f" (auto every {interval//60}m)" if interval > 0 else " (auto-adv disabled)"
            return f"Advertisement sent.{note}"
        return "Advertisement failed -- check logs."

    dispatcher.register_admin_command(
        "!advertise", cmd_advertise,
        help_text="Broadcast bot advertisement to mesh immediately",
        scope="direct",
        category="core",
    )


def _register_prune_cmd(dispatcher: Dispatcher, conn):
    """Register !prune as an admin-only on-demand contact list prune trigger."""

    async def cmd_prune(msg):
        contact_max    = int(conn.config.get("bot.contact_max",    300))
        contact_target = int(conn.config.get("bot.contact_target", 250))
        total = len({k: v for k, v in conn._contacts.items() if len(k) > 12})

        if total < contact_max:
            return (
                f"Contact list has {total} contacts (max={contact_max}) -- "
                "threshold not reached. Prune skipped. "
                "Lower bot.contact_max in config to force a prune."
            )

        await conn._prune_contacts()

        # Re-fetch from radio to get an accurate post-prune count and type
        # breakdown. Reading conn._contacts directly is unreliable here because
        # _refresh_contacts may have fired between the prune releasing its lock
        # and this line executing, repopulating the cache. The radio is the
        # source of truth.
        try:
            from meshcore import EventType
            evt = await conn._mc.commands.get_contacts()
            if evt.type == EventType.ERROR:
                raise RuntimeError(f"get_contacts error: {evt.payload}")
            live = evt.payload or {}
            after = len(live)
            by_type = {}
            for c in live.values():
                t = c.get("type")
                key = f"type{t}" if t is not None else "type?"
                by_type[key] = by_type.get(key, 0) + 1
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        except Exception as e:
            after     = len({k: v for k, v in conn._contacts.items() if len(k) > 12})
            breakdown = f"breakdown unavailable ({e})"

        removed = total - after
        return (
            f"Prune complete: {total} -> {after} contacts "
            f"({removed} removed, target={contact_target}) | "
            f"Remaining: {breakdown}"
        )

    dispatcher.register_admin_command(
        "!prune", cmd_prune,
        help_text="Prune contact list down to target if over threshold",
        scope="direct",
        category="core",
    )


async def shutdown(conn, db):
    """
    Graceful shutdown on SIGINT/SIGTERM from OS or systemctl stop.

    conn.stop() cancels the run() task, which causes main() to return normally.
    asyncio.run() then exits with code 0. Do NOT call sys.exit() or
    loop.stop() here -- both raise inside a task and cause a noisy traceback
    and a non-zero exit code that triggers an unwanted systemd restart.
    """
    logger.info("Shutting down...")
    await conn.stop()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
