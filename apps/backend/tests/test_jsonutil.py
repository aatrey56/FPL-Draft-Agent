"""Strict-JSON writer tests: artifacts must never contain bare NaN."""
import json
import math

import pytest

from backend.ml import jsonutil


def test_nan_and_inf_become_null():
    text = jsonutil.dumps_strict({"a": float("nan"), "b": [1.0, float("inf")], "c": "ok"})
    parsed = json.loads(text)  # strict parse must succeed
    assert parsed == {"a": None, "b": [1.0, None], "c": "ok"}
    assert "NaN" not in text and "Infinity" not in text


def test_finite_values_untouched():
    assert json.loads(jsonutil.dumps_strict({"x": 1.5})) == {"x": 1.5}
    assert not math.isnan(json.loads(jsonutil.dumps_strict({"x": 0.0}))["x"])
