#!/usr/bin/env python3
"""
MeshHall - A modular IRC-style bot for MeshCore mesh networks.
Entry point and main event loop.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"
# Core version is defined in core/__init__.py — edit there, not here.

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


async def main():
    # ── Bootstrap ────────────────────────────────────────────────────────────
    config = Config("config/config.yaml")
    logging.basicConfig(
        level=getattr(logging, config.get("log_level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.get("log_file", "data/meshhall.log")),
        ],
    )
    logger.info(f"MeshHall v{CORE_VERSION} starting up...")

    # ── Dependency version check ──────────────────────────────────────────────
    try:
        from importlib.metadata import version as pkg_version
        from packaging.version import Version
        mc_version = pkg_version("meshcore")
        if Version(mc_version) < Version("2.1.0"):
            logger.error(
                f"meshcore {mc_version} is too old — 2.1.0+ required. "
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

    # ConnectionManager also registers a schema (_dedup, _channels) — same requirement.
    conn = ConnectionManager(config, dispatcher, db)

    # Give the channels plugin a reference to conn so it can call
    # enumerate_channels() from !channel sync and the rehash callback.
    # The plugin registered a placeholder setup() with conn=None; we inject
    # it here by calling the module's _inject_conn() if it exists.
    _inject_conn_to_plugins(loader, conn)

    # Now initialize — core schema + all plugin schemas created here.
    await db.initialize()
    logger.info("Database initialized.")

    # Bootstrap admin privileges
    admin_ids = config.get("bot.admins", [])
    for admin_id in admin_ids:
        await db.upsert_user(admin_id)
        await db.set_privilege(admin_id, 15)
        logger.info(f"Admin bootstrap: {admin_id} set to privilege 15")

    # ── Graceful shutdown + SIGHUP rehash ────────────────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(conn, db)))

    def _sighup_handler():
        logger.info("SIGHUP received — triggering rehash.")
        asyncio.create_task(dispatcher.do_rehash())

    loop.add_signal_handler(signal.SIGHUP, _sighup_handler)

    # Register the system action callback for !restart / !shutdown.
    # Uses os.execv for restart (re-execs the same process — systemd sees a clean
    # exit and restarts per the service Restart= policy). Shutdown sends SIGTERM
    # to self, which the signal handler above catches for graceful teardown.
    # Shutdown does cleanup directly then raises SystemExit(0).
    # Do NOT use asyncio.get_event_loop().stop() from inside a coroutine —
    # Python 3.13's asyncio runner raises RuntimeError("Event loop stopped
    # before Future completed") which exits with code 1, causing systemd to
    # restart the bot even after an intentional !shutdown.
    async def system_action(action: str):
        if action == "restart":
            logger.warning("ADMIN: Restarting bot process via os.execv...")
            await conn.stop()
            await db.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        elif action == "shutdown":
            logger.warning("ADMIN: Shutting down cleanly...")
            await conn.stop()
            await db.close()

    dispatcher.set_system_action_callback(system_action)

    await conn.run()


def _inject_conn_to_plugins(loader: PluginLoader, conn):
    """
    After ConnectionManager is created, give any plugin that has an
    _inject_conn() module-level function a reference to it. This lets
    plugins like 08_channels call conn.enumerate_channels() without
    requiring conn to exist at plugin load time (which would create a
    circular dependency — conn needs db, db needs schemas from plugins).
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


async def shutdown(conn, db):
    """
    Graceful shutdown on SIGINT/SIGTERM from OS or systemctl stop.

    conn.stop() cancels the run() task, which causes main() to return normally.
    asyncio.run() then exits with code 0. Do NOT call sys.exit() or
    loop.stop() here — both raise inside a task and cause a noisy traceback
    and a non-zero exit code that triggers an unwanted systemd restart.
    """
    logger.info("Shutting down...")
    await conn.stop()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
