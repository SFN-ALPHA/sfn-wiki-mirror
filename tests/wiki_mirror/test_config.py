"""Qualifies the mirror settings: defaults, the loopback Qdrant rule, and the API key rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from wiki_mirror.config import (
    API_KEY_ENV,
    MirrorConfigError,
    load_mirror_config,
)

KEY = "k" * 40


@pytest.mark.unit
def test_load_config_absent_file_gives_defaults(tmp_path: Path) -> None:
    config = load_mirror_config(tmp_path / "missing.yaml", environ={})
    assert config.listen_port == 8300
    assert config.api_key is None


@pytest.mark.unit
def test_load_config_reads_the_key_from_the_environment(tmp_path: Path) -> None:
    config = load_mirror_config(tmp_path / "missing.yaml", environ={API_KEY_ENV: KEY})
    assert config.api_key is not None
    assert config.api_key.get_secret_value() == KEY


@pytest.mark.unit
def test_load_config_key_in_the_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mirror.yaml"
    path.write_text(f"api_key: {KEY}\n", encoding="utf-8")
    with pytest.raises(MirrorConfigError, match="environment"):
        load_mirror_config(path, environ={})


@pytest.mark.unit
def test_load_config_short_key_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(MirrorConfigError) as raised:
        load_mirror_config(tmp_path / "missing.yaml", environ={API_KEY_ENV: "short-secret"})
    assert "short-secret" not in str(raised.value)
    assert API_KEY_ENV in str(raised.value)


@pytest.mark.unit
def test_load_config_non_loopback_qdrant_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mirror.yaml"
    path.write_text("qdrant_url: http://192.0.2.5:6333\n", encoding="utf-8")
    with pytest.raises(MirrorConfigError, match="loopback"):
        load_mirror_config(path, environ={})


@pytest.mark.unit
def test_load_config_unknown_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mirror.yaml"
    path.write_text("surprise: 1\n", encoding="utf-8")
    with pytest.raises(MirrorConfigError, match="surprise"):
        load_mirror_config(path, environ={})


@pytest.mark.unit
def test_load_config_budget_above_the_free_tier_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mirror.yaml"
    path.write_text("monthly_chunk_budget: 5000\n", encoding="utf-8")
    with pytest.raises(MirrorConfigError, match="monthly_chunk_budget"):
        load_mirror_config(path, environ={})


@pytest.mark.unit
def test_load_config_non_mapping_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mirror.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(MirrorConfigError, match="mapping"):
        load_mirror_config(path, environ={})
