"""Repository-local entry point for the champion-unit registry audit."""

from __future__ import annotations

import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from rebaser.maintenance.champion_units import main  # noqa: E402


if __name__ == "__main__":
    main()
