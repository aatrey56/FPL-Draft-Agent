"""Shared constants used across the backend."""

import re

# Maps FPL element_type / position_type integer codes to their short labels.
# 1 = Goalkeeper, 2 = Defender, 3 = Midfielder, 4 = Forward.
POSITION_TYPE_LABELS: dict[int, str] = {
    1: "GK",
    2: "DEF",
    3: "MID",
    4: "FWD",
}

# Regex that matches gameweek references in natural-language text.
# Handles: "gw3", "GW 3", "gameweek 3", "game week 3", "week 3",
#          "gw=3", "gw#3", "gw:3" — capturing the numeric portion.
GW_PATTERN: re.Pattern[str] = re.compile(
    r"(?:gw|gameweek|game\s*week|week)\s*[:=#]?\s*(\d{1,2})",
    re.IGNORECASE,
)
