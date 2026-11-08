"""Pytest configuration and fixtures."""

import sys
from unittest.mock import MagicMock

# Mock the zwift module before any imports
sys.modules['zwift'] = MagicMock()

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Keep tests away from the real ~/.cache (route name cache)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
