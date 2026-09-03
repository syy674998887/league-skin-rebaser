"""Repository-local entry point for League Skin Rebaser.

The implementation lives in :mod:`rebaser.app`; this file remains at the
repository root so the established ``uv run script.py`` command keeps working.
When imported, it aliases the implementation module for compatibility with
existing integrations and tests that patch attributes on ``script``.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from rebaser import app as _app  # noqa: E402


if __name__ == "__main__":
    _app.main()
else:
    sys.modules[__name__] = _app
