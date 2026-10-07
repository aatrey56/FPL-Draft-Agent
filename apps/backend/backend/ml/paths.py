"""Season-aware data paths — the one place that knows the on-disk layout.

Layout (see CLAUDE.md §2.1):

* ``<data>/raw/<season>/`` and ``<data>/derived/<season>/`` hold the current
  seasons (2026-27 onward);
* the flat ``<data>/raw/`` and ``<data>/derived/`` trees are the 2025-26
  archive (never overwritten). ``<data>/derived/ml/player_seasons.parquet``
  is the multi-season history table and is valid input for every season.

Every helper takes an optional ``data_root`` (the ``data/`` dir); the CLIs pass
their ``--data-root`` flag through, and only when it is omitted do the helpers
fall back to ``<repo>/data``.

Projections: a season's projection lives at
``<data>/derived/<season>/ml/projections.json``. The 2026-27 preseason
projection predates that convention and sits flat at
``<data>/derived/ml/projections_2627.json``; ``projections_path`` falls back to
it for 2026-27 **only** — any other season without a per-season file raises
rather than silently scoring a new season with last year's projection.
"""

from __future__ import annotations

import re
from pathlib import Path

# The season whose projection predates the per-season layout, and its file.
LEGACY_PROJECTION_SEASON = "2026-27"
LEGACY_PROJECTION_FILE = "projections_2627.json"

_SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")


def default_data_root() -> Path:
    """``<repo>/data`` — used only when a caller passes no ``data_root``."""
    return Path(__file__).resolve().parents[4] / "data"


def _root(data_root: Path | None) -> Path:
    return Path(data_root) if data_root is not None else default_data_root()


def raw_root(season: str, data_root: Path | None = None) -> Path:
    """``<data>/raw/<season>`` — fetcher output for a nested season."""
    return _root(data_root) / "raw" / season


def derived_root(season: str, data_root: Path | None = None) -> Path:
    """``<data>/derived/<season>`` — derived artifacts for a nested season
    (its ``ml/`` subdir holds the panel, xP files and decision artifacts)."""
    return _root(data_root) / "derived" / season


def archive_derived(data_root: Path | None = None) -> Path:
    """``<data>/derived`` — the flat 2025-26 archive (read-only)."""
    return _root(data_root) / "derived"


def seasons_table_path(data_root: Path | None = None) -> Path:
    """The multi-season ``player_seasons.parquet`` history (flat archive)."""
    return archive_derived(data_root) / "ml" / "player_seasons.parquet"


def prior_season(season: str) -> str:
    """The season before ``season``: ``"2026-27"`` -> ``"2025-26"``.

    Raises ValueError for anything not shaped ``YYYY-YY`` with consecutive
    years (``"2099-00"`` is valid: it follows ``"2098-99"``).
    """
    match = _SEASON_RE.match(season)
    if not match or (int(match.group(1)) + 1) % 100 != int(match.group(2)):
        raise ValueError(f"not a season label: {season!r} (expected e.g. '2026-27')")
    start = int(match.group(1)) - 1
    return f"{start}-{(start + 1) % 100:02d}"


def season_projections_path(season: str, data_root: Path | None = None) -> Path:
    """Where ``season``'s projection is expected (whether or not it exists)."""
    return derived_root(season, data_root) / "ml" / "projections.json"


def projections_path(season: str, data_root: Path | None = None) -> Path:
    """The projection file for ``season``.

    ``<data>/derived/<season>/ml/projections.json`` when it exists; else, for
    2026-27 only, the flat preseason ``projections_2627.json``. Any other
    season without its own file raises FileNotFoundError naming the
    expected per-season path.
    """
    expected = season_projections_path(season, data_root)
    if expected.exists():
        return expected
    if season == LEGACY_PROJECTION_SEASON:
        return archive_derived(data_root) / "ml" / LEGACY_PROJECTION_FILE
    raise FileNotFoundError(f"no projection for season {season}: expected {expected}")
