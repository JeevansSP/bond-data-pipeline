"""Tests for settings."""

from __future__ import annotations

from pathlib import Path

from bonds.config import REPO_ROOT, DatabaseSettings, Settings


def test_database_url_uses_psycopg_driver() -> None:
    db = DatabaseSettings(host="h", port=1234, user="u", password="p", name="n")
    assert db.url == "postgresql+psycopg://u:p@h:1234/n"


def test_relative_data_dir_is_anchored_to_repo_root() -> None:
    settings = Settings(data_root=Path("data"))
    assert settings.data_dir == REPO_ROOT / "data"


def test_absolute_data_dir_is_preserved(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path)
    assert settings.data_dir == tmp_path
