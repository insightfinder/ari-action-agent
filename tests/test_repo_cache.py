import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.tools import repo_cache  # noqa: E402
from ari_agent.tools.git_ops import authed_url, clone  # noqa: E402


def _run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo_dir, filename, content, message):
    (repo_dir / filename).write_text(content)
    _run("add", ".", cwd=repo_dir)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", message, cwd=repo_dir)


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _run("init", "-q", "-b", "main", cwd=src)
    _commit(src, "a.txt", "v1\n", "init")
    return src


@pytest.fixture(autouse=True)
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "cache-root"
    fake_settings = SimpleNamespace(repo_cache_root=str(root))
    monkeypatch.setattr(repo_cache, "get_settings", lambda: fake_settings)
    return root


@pytest.mark.asyncio
async def test_ensure_cache_creates_mirror_on_first_call(source_repo, cache_root):
    cache_dir = await repo_cache.ensure_cache({"url": str(source_repo)})

    assert Path(cache_dir).is_dir()
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=cache_dir, capture_output=True, text=True, check=True
    )
    assert "init" in result.stdout


@pytest.mark.asyncio
async def test_ensure_cache_pulls_latest_on_second_call(source_repo, cache_root):
    cache_dir_1 = await repo_cache.ensure_cache({"url": str(source_repo)})

    _commit(source_repo, "b.txt", "v2\n", "second commit")

    cache_dir_2 = await repo_cache.ensure_cache({"url": str(source_repo)})

    assert cache_dir_1 == cache_dir_2  # same repo -> same cache slot
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=cache_dir_2, capture_output=True, text=True, check=True
    )
    assert "second commit" in result.stdout


@pytest.mark.asyncio
async def test_different_repos_get_different_cache_slots(tmp_path, cache_root):
    src_a = tmp_path / "a"
    src_a.mkdir()
    _run("init", "-q", "-b", "main", cwd=src_a)
    _commit(src_a, "f.txt", "a\n", "init a")

    src_b = tmp_path / "b"
    src_b.mkdir()
    _run("init", "-q", "-b", "main", cwd=src_b)
    _commit(src_b, "f.txt", "b\n", "init b")

    cache_a = await repo_cache.ensure_cache({"url": str(src_a)})
    cache_b = await repo_cache.ensure_cache({"url": str(src_b)})

    assert cache_a != cache_b


@pytest.mark.asyncio
async def test_two_requests_against_same_cached_repo_edit_independently(source_repo, cache_root, tmp_path):
    # This is the actual requirement: multiple requests sharing one cache
    # must each get an independent working copy — editing one must never
    # affect the other, or the shared cache itself.
    cache_dir = await repo_cache.ensure_cache({"url": str(source_repo)})

    ws1 = tmp_path / "request-1"
    ws1.mkdir()
    ws2 = tmp_path / "request-2"
    ws2.mkdir()

    repo1 = await clone({"url": cache_dir, "ref": "main"}, str(ws1))
    repo2 = await clone({"url": cache_dir, "ref": "main"}, str(ws2))

    (Path(repo1) / "a.txt").write_text("edited by request 1\n")
    (Path(repo2) / "a.txt").write_text("edited by request 2\n")

    assert (Path(repo1) / "a.txt").read_text() == "edited by request 1\n"
    assert (Path(repo2) / "a.txt").read_text() == "edited by request 2\n"
    # The cache itself is a bare mirror (no working tree to edit at all) —
    # confirm neither per-request clone changed its state.
    cache_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cache_dir, capture_output=True, text=True, check=True
    ).stdout.strip()
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert cache_head == source_head


@pytest.mark.asyncio
async def test_cache_origin_is_scrubbed_after_initial_clone(source_repo, cache_root):
    cache_dir = await repo_cache.ensure_cache({"url": str(source_repo)})

    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=cache_dir, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == str(source_repo)


def test_authed_url_only_transforms_when_pat_present():
    assert authed_url("https://github.com/x/y", {}) == "https://github.com/x/y"
    assert authed_url("https://github.com/x/y", {"pat": "tok"}) == "https://x-access-token:tok@github.com/x/y"
