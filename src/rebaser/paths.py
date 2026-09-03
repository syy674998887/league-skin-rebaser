"""Canonical repository and runtime paths.

The application is intentionally a repository-local Windows tool: downloaded
native tools, mutable configuration, generated output, and versioned data all
live beside ``pyproject.toml``.  Keeping root discovery here prevents module
moves inside ``src/rebaser`` from silently changing those locations.
"""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
_DEFAULT_PROJECT_ROOT = SOURCE_ROOT.parent
PROJECT_ROOT = Path(
    os.environ.get("LEAGUE_SKIN_REBASER_PROJECT_ROOT", _DEFAULT_PROJECT_ROOT)
).resolve()

DATA_ROOT = PROJECT_ROOT / "data"
