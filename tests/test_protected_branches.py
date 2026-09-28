import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_oncall_agent.protected_branches as protected_branches_module  # noqa: E402
from ari_oncall_agent.protected_branches import is_protected, protected_branches, repo_slug  # noqa: E402


def test_repo_slug_normalizes_scheme_and_dot_git_suffix():
    assert repo_slug("https://github.com/insightfinder/InfrastructureCore") == "insightfinder/infrastructurecore"
    assert repo_slug("https://github.com/insightfinder/InfrastructureCore.git") == "insightfinder/infrastructurecore"
    assert repo_slug("http://github.com/InsightFinder/InfrastructureCore") == "insightfinder/infrastructurecore"


def test_configured_repo_reports_its_own_branches_plus_the_defaults():
    # From configs/oncall/protected_branches.yaml -- a real repo entry,
    # not a fixture, since this file is the actual configurable blacklist.
    # main/master come from default_protected_branches, added on top of
    # (not instead of) this repo's own dev/staging/test entry.
    branches = protected_branches("https://github.com/insightfinder/InfrastructureCore")
    assert branches == {"dev", "staging", "test", "main", "master"}


def test_protected_branches_matches_regardless_of_dot_git_suffix_or_scheme():
    assert is_protected("https://github.com/insightfinder/InfrastructureCore.git", "dev")
    assert is_protected("http://github.com/insightfinder/InfrastructureCore", "staging")


def test_main_and_master_are_protected_by_default_for_the_configured_repo_too():
    assert is_protected("https://github.com/insightfinder/InfrastructureCore", "main")
    assert is_protected("https://github.com/insightfinder/InfrastructureCore", "master")


@pytest.fixture
def custom_config(tmp_path, monkeypatch):
    """Points the loader at a throwaway config file instead of the real
    configs/oncall/protected_branches.yaml, clearing the lru_cache before
    and after so tests never see each other's or the real file's state."""

    def _write(content: str):
        path = tmp_path / "protected_branches.yaml"
        path.write_text(content)
        monkeypatch.setattr(protected_branches_module, "config_file", lambda name: path)
        protected_branches_module._load.cache_clear()

    yield _write
    protected_branches_module._load.cache_clear()


def test_default_protected_branches_can_be_explicitly_emptied(custom_config):
    # An explicit `[]` is a real ops decision to disable the default
    # (distinct from just omitting the key, which keeps main/master
    # protected) -- see _load()'s own `is not None` check for why.
    custom_config("default_protected_branches: []\nprotected_branches: {}\n")

    assert protected_branches("https://github.com/some-org/any-repo") == frozenset()


def test_default_protected_branches_is_itself_configurable(custom_config):
    custom_config("default_protected_branches: [release]\nprotected_branches: {}\n")

    assert protected_branches("https://github.com/some-org/any-repo") == {"release"}
    assert not is_protected("https://github.com/some-org/any-repo", "main")


def test_unconfigured_repo_gets_exactly_the_configured_defaults(custom_config):
    # No entry at all under protected_branches: -- must still get
    # default_protected_branches, not an empty blacklist. Uses an isolated
    # config (not the real configs/oncall/protected_branches.yaml) so this
    # doesn't depend on that file's current default list.
    custom_config("default_protected_branches: [main, master]\nprotected_branches: {}\n")

    assert protected_branches("https://github.com/some-org/unrelated-repo") == {"main", "master"}
    assert is_protected("https://github.com/some-org/unrelated-repo", "main")
    assert not is_protected("https://github.com/some-org/unrelated-repo", "dev")


def test_missing_config_file_still_protects_main_and_master(tmp_path, monkeypatch):
    monkeypatch.setattr(protected_branches_module, "config_file", lambda name: tmp_path / "does-not-exist.yaml")
    protected_branches_module._load.cache_clear()
    try:
        assert protected_branches("https://github.com/some-org/any-repo") == {"main", "master"}
    finally:
        protected_branches_module._load.cache_clear()
