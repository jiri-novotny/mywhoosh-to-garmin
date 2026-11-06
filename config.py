"""Locate and load the configuration file.

Lookup order (first existing file wins):
1. explicit path (--config) or $MYWHOOSH_TO_GARMIN_CONFIG
2. $XDG_CONFIG_HOME/mywhoosh-to-garmin/config.env (default ~/.config/mywhoosh-to-garmin/config.env)
3. ./.env in the current directory (legacy)

Variables already set in the environment take precedence over the file.
"""

import os
import stat
from typing import List, Optional

from dotenv import load_dotenv

APP_NAME = "mywhoosh-to-garmin"
CONFIG_ENV_VAR = "MYWHOOSH_TO_GARMIN_CONFIG"
CONFIG_FILENAME = "config.env"


def user_config_path() -> str:
    base = os.getenv("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME, CONFIG_FILENAME)


def config_candidates(explicit: Optional[str] = None) -> List[str]:
    candidates = []
    explicit = explicit or os.getenv(CONFIG_ENV_VAR)
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    candidates.append(user_config_path())
    candidates.append(os.path.join(os.getcwd(), ".env"))
    return candidates


def load_config_file(explicit: Optional[str] = None) -> Optional[str]:
    """Load the first config file found. Returns its path, or None if none exists.

    Raises:
        FileNotFoundError: If an explicitly requested file does not exist
    """
    explicit = explicit or os.getenv(CONFIG_ENV_VAR)
    if explicit and not os.path.isfile(os.path.expanduser(explicit)):
        raise FileNotFoundError(f"Config file not found: {explicit}")
    for path in config_candidates(explicit):
        if os.path.isfile(path):
            load_dotenv(path)
            return path
    return None


def is_world_readable(path: str) -> bool:
    """True if group/others can read the file (it contains passwords)."""
    return bool(os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH))
