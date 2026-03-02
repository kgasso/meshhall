"""
PluginLoader — discovers and initialises plugin modules.

Each plugin is a Python file in the plugins/ directory that exposes:

    def setup(dispatcher, config, db) -> None

Plugins may also declare a module-level version string:

    VERSION = "1.0.0"

Load order is alphabetical; prefix filenames with numbers to control order
(e.g. 01_time.py, 02_checkin.py).

To disable a plugin, prefix with underscore: _disabled_plugin.py
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class PluginLoader:
    def __init__(self, dispatcher, config, db):
        self.dispatcher = dispatcher
        self.config = config
        self.db = db
        # name → (version_str, path)
        self._loaded: Dict[str, Tuple[str, str]] = {}

    def load_all(self, plugin_dir: str):
        directory = Path(plugin_dir)
        if not directory.exists():
            logger.warning(f"Plugin directory '{plugin_dir}' not found.")
            return

        plugin_files = sorted(directory.glob("*.py"))
        loaded = 0
        for path in plugin_files:
            if path.name.startswith("_") or path.name == "__init__.py":
                continue
            try:
                self._load(path)
                loaded += 1
            except Exception as e:
                logger.error(f"Failed to load plugin '{path.name}': {e}", exc_info=True)

        logger.info(f"Loaded {loaded} plugin(s) from '{plugin_dir}'.")

    def _load(self, path: Path):
        module_name = f"plugins.{path.stem}"
        spec   = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        if not hasattr(module, "setup"):
            raise AttributeError(f"Plugin '{path.name}' has no setup() function.")

        module.setup(self.dispatcher, self.config, self.db)

        version = getattr(module, "__version__", "?.?.?")
        self._loaded[path.stem] = (version, str(path))
        logger.info(f"Plugin loaded: {path.name} v{version}")

    @property
    def loaded_plugins(self) -> Dict[str, Tuple[str, str]]:
        """Returns {stem: (version, path)} for all successfully loaded plugins."""
        return dict(self._loaded)
