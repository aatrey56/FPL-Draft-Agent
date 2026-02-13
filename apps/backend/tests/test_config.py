import os

from backend.config import Settings, _resolve_dir


def test_resolve_dir_relative(tmp_path) -> None:
    repo_root = str(tmp_path)
    abs_path, rel_path = _resolve_dir("reports", repo_root)
    assert abs_path == os.path.join(repo_root, "reports")
    assert rel_path == "reports"


def test_settings_defaults_are_populated() -> None:
    settings = Settings()
    assert settings.reports_dir
    assert settings.data_dir
    assert settings.web_dir
