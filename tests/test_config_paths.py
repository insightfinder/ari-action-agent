"""Tests for locating configs/.

The bug these cover: both registries resolved the directory as
`Path(__file__).resolve().parents[3] / "configs"`, a fixed number of hops that
is only correct when running from the source tree. `pip install .` (what the
Dockerfile does) moves the package into site-packages, where parents[3] is
`/usr/local/lib/python3.<minor>` — nowhere near the `/app/configs` the image
actually ships. Every containerised run died in its first activity with
FileNotFoundError, so no workflow could complete, while the API process
(which never loads a registry at import time) looked perfectly healthy.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent import config_paths  # noqa: E402


def test_finds_the_repos_own_configs_from_the_source_tree():
    # No override, no cwd games: the checkout this test runs from must resolve.
    found = config_paths.configs_dir()
    assert (found / "capabilities.yaml").is_file()
    assert (found / "tools.yaml").is_file()


def test_env_override_wins_over_everything_else(tmp_path, monkeypatch):
    (tmp_path / "capabilities.yaml").write_text("capabilities: {}")
    monkeypatch.setenv("ARI_CONFIGS_DIR", str(tmp_path))
    assert config_paths.configs_dir() == tmp_path


def test_env_override_is_ignored_when_it_does_not_exist(tmp_path, monkeypatch):
    # A stale/typo'd override must not shadow a working default -- falling
    # through is what keeps a misconfigured deployment bootable.
    monkeypatch.setenv("ARI_CONFIGS_DIR", str(tmp_path / "nope"))
    assert config_paths.configs_dir() != tmp_path / "nope"
    assert (config_paths.configs_dir() / "capabilities.yaml").is_file()


def test_falls_back_to_the_working_directory(tmp_path, monkeypatch):
    # This is the case that matters in the image: the package lives in
    # site-packages, so no package-relative hop can reach configs/, but the
    # Dockerfile's WORKDIR does contain it.
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "capabilities.yaml").write_text("capabilities: {}")
    monkeypatch.delenv("ARI_CONFIGS_DIR", raising=False)
    monkeypatch.setattr(
        config_paths, "_candidates",
        lambda: [Path("/definitely/not/here"), Path.cwd() / "configs"])
    monkeypatch.chdir(tmp_path)
    assert config_paths.configs_dir() == configs


def test_error_names_every_location_it_tried(monkeypatch):
    # The original failure was a bare FileNotFoundError naming one path that
    # meant nothing to whoever read it; the message has to say where to look.
    tried = [Path("/nope/one"), Path("/nope/two")]
    monkeypatch.setattr(config_paths, "_candidates", lambda: list(tried))
    with pytest.raises(FileNotFoundError) as exc_info:
        config_paths.configs_dir()
    message = str(exc_info.value)
    # str(Path) so the assertion holds on Windows separators too.
    for path in tried:
        assert str(path) in message
    assert "ARI_CONFIGS_DIR" in message, "must say how to fix it"


def test_config_file_joins_onto_the_resolved_directory():
    assert config_paths.config_file("tools.yaml") == config_paths.configs_dir() / "tools.yaml"
