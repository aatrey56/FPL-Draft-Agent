"""Shared constants used across the backend."""

# Maps FPL element_type / position_type integer codes to their short labels.
# 1 = Goalkeeper, 2 = Defender, 3 = Midfielder, 4 = Forward.
POSITION_TYPE_LABELS: dict[int, str] = {
    1: "GK",
    2: "DEF",
    3: "MID",
    4: "FWD",
}
