"""Qualifies the mirror settings: defaults, the loopback Qdrant rule, and the API key rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from wiki_mirror.config import (
    API_KEY_ENV,
    MirrorConfig,
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


def _load(tmp_path: Path, text: str) -> MirrorConfig:
    """Load a settings file holding `text`."""
    path = tmp_path / "mirror.yaml"
    path.write_text(text, encoding="utf-8")
    return load_mirror_config(path, environ={})


@pytest.mark.unit
def test_load_config_embedding_defaults_are_the_measured_ones(tmp_path: Path) -> None:
    config = load_mirror_config(tmp_path / "missing.yaml", environ={})
    assert config.embedding_url is None
    assert (config.embedding_model, config.embedding_dimensions) == ("bge-m3", 1024)
    assert (config.embedding_concurrency, config.embedding_batch) == (16, 8)
    assert config.embedding_headers == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("http://192.0.2.5:8080", "http://192.0.2.5:8080"),
        ("https://embed.example.test/", "https://embed.example.test"),
        ("http://192.0.2.5:8080/base/", "http://192.0.2.5:8080/base"),
    ],
)
def test_load_config_embedding_url_accepts_http_and_https_and_strips_the_slash(
    tmp_path: Path, given: str, kept: str
) -> None:
    assert _load(tmp_path, f"embedding_url: {given}\n").embedding_url == kept


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "ftp://192.0.2.5/",
        "192.0.2.5:8080",
        "http:///hunter2",
        "http://user:hunter2@192.0.2.5:8080",
        "http://hunter2@192.0.2.5:8080",
        "http://192.0.2.5:8080?token=hunter2",
        "http://192.0.2.5:8080/#hunter2",
        "http://192.0.2.5:notaport/hunter2",
    ],
)
def test_load_config_embedding_url_that_is_not_a_plain_origin_is_refused_without_echo(
    tmp_path: Path, bad: str
) -> None:
    with pytest.raises(MirrorConfigError) as raised:
        _load(tmp_path, f'embedding_url: "{bad}"\n')
    assert "embedding_url" in str(raised.value)
    assert "hunter2" not in str(raised.value)
    assert bad not in str(raised.value)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Authorization", "authorization", "Proxy-Authorization"])
def test_load_config_embedding_headers_refuse_credentials_without_echoing_the_value(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(MirrorConfigError) as raised:
        _load(tmp_path, f'embedding_headers: {{"{name}": "Bearer hunter2"}}\n')
    assert "EMBEDDING_API_KEY" in str(raised.value)
    assert "hunter2" not in str(raised.value)


@pytest.mark.unit
def test_load_config_embedding_headers_accept_a_priority_header(tmp_path: Path) -> None:
    config = _load(tmp_path, 'embedding_headers: {X-Priority: "3"}\n')
    assert config.embedding_headers == {"X-Priority": "3"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    ['{"Bad Name": "1"}', '{"": "1"}', '{"X-A": "line\\nbreak"}', '{"X-A:": "1"}'],
)
def test_load_config_embedding_headers_that_are_not_valid_http_are_refused(
    tmp_path: Path, headers: str
) -> None:
    with pytest.raises(MirrorConfigError, match="embedding_headers"):
        _load(tmp_path, f"embedding_headers: {headers}\n")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["embedding_headers", "query_embedding_headers"])
def test_load_config_embedding_headers_with_a_non_ascii_value_are_refused_without_echoing_it(
    tmp_path: Path, field: str
) -> None:
    # httpx cannot encode such a value, so every request would fail; refuse it at startup.
    with pytest.raises(MirrorConfigError, match=field) as raised:
        _load(tmp_path, f'{field}: {{"X-Priority": "caf\\u00e9"}}\n')
    assert "printable ASCII" in str(raised.value)
    assert "caf" not in str(raised.value)


@pytest.mark.unit
def test_load_config_query_embedding_headers_default_to_none_and_are_separate(
    tmp_path: Path,
) -> None:
    config = _load(tmp_path, 'embedding_headers: {X-Priority: "3"}\n')
    assert config.query_embedding_headers == {}


@pytest.mark.unit
def test_load_config_query_embedding_headers_accept_a_priority_header(tmp_path: Path) -> None:
    config = _load(tmp_path, 'query_embedding_headers: {X-Priority: "1"}\n')
    assert config.query_embedding_headers == {"X-Priority": "1"}


@pytest.mark.unit
def test_load_config_query_embedding_headers_refuse_credentials_without_echoing_the_value(
    tmp_path: Path,
) -> None:
    with pytest.raises(MirrorConfigError) as raised:
        _load(tmp_path, 'query_embedding_headers: {Authorization: "Bearer hunter2"}\n')
    assert "EMBEDDING_API_KEY" in str(raised.value)
    assert "hunter2" not in str(raised.value)


@pytest.mark.unit
@pytest.mark.parametrize("headers", ['{"Bad Name": "1"}', '{"X-A": "line\\nbreak"}'])
def test_load_config_query_embedding_headers_that_are_not_valid_http_are_refused(
    tmp_path: Path, headers: str
) -> None:
    with pytest.raises(MirrorConfigError, match="query_embedding_headers"):
        _load(tmp_path, f"query_embedding_headers: {headers}\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    "setting", ["embedding_concurrency: 0", "embedding_concurrency: 65", "embedding_batch: 0",
                "embedding_batch: 65", "embedding_dimensions: 0"]
)
def test_load_config_embedding_numbers_outside_their_range_are_refused(
    tmp_path: Path, setting: str
) -> None:
    with pytest.raises(MirrorConfigError, match=setting.split(":")[0]):
        _load(tmp_path, f"{setting}\n")
