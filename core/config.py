"""
Configuration manager — loads YAML config with dot-path access and defaults.

Plugin configs live in config/plugins/<plugin_name>.yaml and are loaded
on demand only when a plugin calls config.plugin("name"). A plugin config
file that doesn't exist simply returns an empty dict — no errors, no side
effects from unloaded plugins having stale config files around.

Hot reload:
    Call config.reload() to re-read all YAML files from disk. The plugin
    cache is cleared so the next config.plugin() call re-reads from disk.
    Plugins that read config lazily (inside command handlers) pick up
    changes automatically. Background loops check config on each iteration.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import logging
import yaml
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Config:
    def __init__(self, path: str):
        self._path = Path(path)
        self._plugin_dir = self._path.parent / "plugins"
        self._data: dict = {}
        self._plugin_cache: dict = {}
        self._load()

    def _load(self):
        """Read main config from disk and clear plugin cache."""
        if self._path.exists():
            with open(self._path) as f:
                self._data = yaml.safe_load(f) or {}
        else:
            logger.warning(f"Config file not found: {self._path}. Using defaults.")
            self._data = {}
        self._plugin_cache.clear()

    def reload(self):
        """
        Re-read all config files from disk without restarting the bot.
        Returns a list of (filename, error_message) for any files with parse errors.
        The main config is rolled back if it fails to parse.
        Plugin configs are cleared from cache and re-read lazily on next access.
        """
        errors = []
        old_data = self._data.copy()
        old_cache = dict(self._plugin_cache)

        try:
            self._load()  # clears plugin cache as a side effect
            logger.info(f"Config reloaded from {self._path}")
        except Exception as e:
            self._data = old_data
            self._plugin_cache = old_cache
            errors.append((str(self._path), str(e)))
            logger.error(f"Failed to reload main config: {e}")
            return errors

        # Pre-validate any plugin configs that were previously loaded
        # so YAML errors are surfaced immediately rather than at next access
        for name in list(old_cache.keys()):
            config_path = self._plugin_dir / f"{name}.yaml"
            if config_path.exists():
                try:
                    with open(config_path) as f:
                        yaml.safe_load(f)
                except Exception as e:
                    errors.append((str(config_path), str(e)))
                    logger.error(f"Plugin config '{name}' has YAML errors after reload: {e}")

        return errors

    def get(self, key: str, default: Any = None) -> Any:
        """Support dot-notation keys like 'connection.serial_port'."""
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def section(self, key: str) -> dict:
        val = self.get(key, {})
        return val if isinstance(val, dict) else {}

    def plugin(self, name: str) -> "PluginConfig":
        """
        Load and return config for a plugin by name.
        Looks for config/plugins/<n>.yaml — returns empty config if not found.
        Cache is cleared on reload() so changes are picked up on next access.
        """
        if name not in self._plugin_cache:
            config_path = self._plugin_dir / f"{name}.yaml"
            data = {}
            if config_path.exists():
                try:
                    with open(config_path) as f:
                        data = yaml.safe_load(f) or {}
                    logger.debug(f"Loaded plugin config: {config_path}")
                except Exception as e:
                    logger.error(f"Failed to load plugin config '{config_path}': {e}")
            self._plugin_cache[name] = PluginConfig(name, data)
        return self._plugin_cache[name]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)


class PluginConfig:
    """Thin wrapper around a plugin's config dict with the same get() interface."""

    def __init__(self, name: str, data: dict):
        self._name = name
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def section(self, key: str) -> dict:
        val = self.get(key, {})
        return val if isinstance(val, dict) else {}

    def __bool__(self):
        return bool(self._data)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)
