"""Kalshi 15-min crypto signals bot."""
from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("kalshi-bot")
except metadata.PackageNotFoundError:
    # Not installed (e.g. running straight from source) - keep in sync with pyproject.toml.
    __version__ = "0.5.0"
