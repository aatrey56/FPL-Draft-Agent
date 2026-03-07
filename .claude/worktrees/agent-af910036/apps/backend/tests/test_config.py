import os

import pytest

from backend.config import Settings, _require_int_env, _resolve_dir


def test_resolve_dir_relative(tmp_path) -> None:
    repo_root = str(tmp_path)
    abs_path, rel_path = _resolve_dir("reports", repo_root)
    assert abs_path == os.path.join(repo_root, "reports")
    assert rel_path == "reports"


def test_settings_defaults_are_populated(monkeypatch) -> None:
    monkeypatch.setenv("LEAGUE_ID", "11111")
    monkeypatch.setenv("ENTRY_ID", "22222")
    settings = Settings()
    assert settings.reports_dir
    assert settings.data_dir
    assert settings.web_dir
    assert settings.league_id == 11111
    assert settings.entry_id == 22222


def test_settings_missing_league_id_raises(monkeypatch) -> None:
    monkeypatch.delenv("LEAGUE_ID", raising=False)
    monkeypatch.setenv("ENTRY_ID", "22222")
    with pytest.raises(ValueError, match="LEAGUE_ID"):
        Settings()


def test_settings_missing_entry_id_raises(monkeypatch) -> None:
    monkeypatch.setenv("LEAGUE_ID", "11111")
    monkeypatch.delenv("ENTRY_ID", raising=False)
    with pytest.raises(ValueError, match="ENTRY_ID"):
        Settings()


def test_require_int_env_valid(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_INT_VAR", "42")
    assert _require_int_env("_TEST_INT_VAR") == 42


def test_require_int_env_missing_raises(monkeypatch) -> None:
    monkeypatch.delenv("_TEST_INT_VAR", raising=False)
    with pytest.raises(ValueError, match="_TEST_INT_VAR.*required"):
        _require_int_env("_TEST_INT_VAR")


def test_require_int_env_non_integer_raises(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_INT_VAR", "notanint")
    with pytest.raises(ValueError, match="must be an integer"):
        _require_int_env("_TEST_INT_VAR")
