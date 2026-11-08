"""Cache of MyWhoosh route id -> ride name.

MyWhoosh FIT files contain the route id (developer field "CurrentRouteId") but
their "Title" field is corrupted, so the name is only available from the API
listing. Every ride downloaded through the API records its name here, so
offline files (--file) of routes ridden before get the same name.
"""

import json
import logging
import os
from typing import Dict, Optional


def default_cache_path() -> str:
    base = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "mywhoosh-to-garmin", "routes.json")


class RouteNameCache:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_cache_path()
        self.logger = logging.getLogger(__name__)
        self._names: Dict[str, str] = {}
        try:
            with open(self.path) as f:
                self._names = {str(k): v for k, v in json.load(f).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.logger.warning(f"Ignoring unreadable route name cache {self.path}: {e}")

    def get(self, route_id: Optional[int]) -> Optional[str]:
        return None if route_id is None else self._names.get(str(route_id))

    def set(self, route_id: Optional[int], name: Optional[str]) -> None:
        if route_id is None or not name or self._names.get(str(route_id)) == name:
            return
        self._names[str(route_id)] = name
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._names, f, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            self.logger.warning(f"Could not save route name cache {self.path}: {e}")
