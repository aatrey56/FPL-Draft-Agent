"""Strict-JSON helpers shared by the ML pipeline writers.

Python's ``json.dumps`` emits bare ``NaN`` for float('nan'), which is invalid
JSON and rejected by strict parsers (including Go's encoding/json — the
serving layer). Every artifact consumed by the Go tools must be written
through :func:`dumps_strict`, which converts NaN/Inf to null and refuses to
emit non-finite literals.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def dumps_strict(value: Any, **kwargs: Any) -> str:
    """json.dumps with NaN/Inf replaced by null; guaranteed strict-parseable."""
    return json.dumps(_sanitize(value), allow_nan=False, **kwargs)
