"""`set_startup_probe`: more time to start for one container of a Deployment, end to end against the fake API.

Uses the fake cluster of test_k8s_change_actions.py. Its `checkout` has a startup probe (initialDelaySeconds 60,
10 x 10s: 160s); the tests give it a liveness probe whose exec check carries the canary, or remove its startup
probe, as each case needs.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
import test_k8s_change_actions as ca  # noqa: E402
from test_k8s_capabilities import _change  # noqa: E402

from common.capabilities_base import StepRefused  # noqa: E402
from k8s_agent.tools import policy, views  # noqa: E402

LIVENESS = {"exec": {"command": ["sh", "-c", f"curl -f --user admin:{fx.CANARY} localhost:15672/health"]}, "periodSeconds": 30,
            "failureThreshold": 6, "initialDelaySeconds": 120, "timeoutSeconds": 20}


@pytest.fixture
def cluster(monkeypatch):
    # The worker's key for check fingerprints (its payload encryption key); not set in unit tests otherwise.
    monkeypatch.setattr(views, "_digest_key", lambda: b"k" * 32)
    return ca.build_cluster(monkeypatch)


def _container(cluster):
    return ca._obj(cluster, "Deployment", "checkout")["spec"]["template"]["spec"]["containers"][0]


def _no_startup_probe(cluster, liveness=True):
    c = _container(cluster)
    c.pop("startupProbe", None)
    if liveness:
        c["livenessProbe"] = json.loads(json.dumps(LIVENESS))
    return c


def _time(quote):
    return _change(action="set_startup_probe", startup_time_quote=quote)


@pytest.mark.parametrize(
    "prompt,asked",
    [
        ("give deployment checkout 20 minutes to start", True),
        ("checkout needs 10 minutes to start up, add a startup probe", True),
        ("set the startupProbe failureThreshold of checkout to 60", True),
        ("checkout 启动需要 10 分钟，加一个启动探针", True),
        ("checkout restarted 5 times, fix it", False),
        ("set the liveness timeout of checkout to 30s", False),
    ],
)
def test_asks_startup_change(prompt, asked):
    assert policy.asks_startup_change(prompt) is asked


@pytest.mark.asyncio
async def test_a_startup_probe_is_added_by_copying_the_liveness_check(cluster):
    _no_startup_probe(cluster)
    prompt = "checkout needs 20 minutes to start, add a startup probe"
    result = await ca._propose(prompt, _time("20 minutes"))
    pa = result["pending_action"]
    assert pa["action"] == "set_startup_probe" and pa["container"] == "app"
    # 20 min at the liveness probe's 30s period.
    assert (pa["failure_threshold"], pa["period_seconds"]) == (40, 30)
    assert pa["copy_check_from"] == "livenessProbe" and pa["check_digest"]
    assert pa["after"]["startupProbe"].startswith("exec check copied from the liveness probe")
    assert pa["risk_tier"] == "medium"
    assert any("progressDeadlineSeconds" in r["message"] for r in pa["risks"])
    assert any(c["field"] == "startupProbe" for c in pa["persistence"]["changes"])
    # The check is copied into the dry run, never into the proposal.
    [dry] = cluster.patches
    probe = dry["body"]["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
    assert dry["dry_run"] and probe["exec"] == LIVENESS["exec"] and probe["failureThreshold"] == 40 and probe["periodSeconds"] == 30
    assert probe["initialDelaySeconds"] == 0 and probe["timeoutSeconds"] == 20
    assert fx.CANARY not in json.dumps(result)


@pytest.mark.asyncio
async def test_adding_one_is_applied_once_and_verified(cluster):
    _no_startup_probe(cluster)
    pa = await ca._approved(cluster, "give checkout 20 minutes to start", _time("20 minutes"))
    cluster.auto_rollout = True
    result = await ca._apply(pa)
    assert result["status"] == "succeeded"
    assert _container(cluster)["startupProbe"]["exec"] == LIVENESS["exec"]
    assert fx.CANARY not in json.dumps(result)
    cluster.patches.clear()
    again = await ca._apply(pa, attempt=2)
    assert again["applied"]["already_at_target"] is True and not cluster.patches


@pytest.mark.asyncio
async def test_a_changed_liveness_check_is_not_copied(cluster):
    _no_startup_probe(cluster)
    pa = await ca._approved(cluster, "give checkout 20 minutes to start", _time("20 minutes"))
    _container(cluster)["livenessProbe"]["exec"]["command"] = ["true"]
    with pytest.raises(RuntimeError, match="probes changed since the proposal"):
        await ca._apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_existing_startup_probe_gets_more_time_and_keeps_its_check(cluster):
    _container(cluster)["startupProbe"]["httpGet"] = {"path": "/ready", "port": 8080}
    pa = await ca._approved(cluster, "give checkout 10 minutes to start", _time("10 minutes"))
    # (600 - initialDelaySeconds 60) / periodSeconds 10.
    assert (pa["failure_threshold"], pa["period_seconds"], pa["copy_check_from"]) == (54, 10, None)
    cluster.auto_rollout = True
    result = await ca._apply(pa)
    assert result["status"] == "succeeded"
    probe = _container(cluster)["startupProbe"]
    assert probe["failureThreshold"] == 54 and probe["httpGet"] == {"path": "/ready", "port": 8080}


@pytest.mark.asyncio
async def test_failure_threshold_stated_in_the_request(cluster):
    result = await ca._propose(
        "set the startupProbe failureThreshold of checkout to 30",
        _change(action="set_startup_probe", failure_threshold=30, failure_threshold_quote="failureThreshold of checkout to 30"),
    )
    assert result["pending_action"]["failure_threshold"] == 30


@pytest.mark.asyncio
async def test_less_time_is_refused(cluster):
    result = await ca._propose("give checkout 2 minutes to start", _time("2 minutes"))
    assert "only gives a container more time to start, never less" in result["refusal"]["reason"]
    assert not cluster.patches


@pytest.mark.asyncio
async def test_no_probe_to_copy_is_refused(cluster):
    _no_startup_probe(cluster, liveness=False)
    _container(cluster).pop("readinessProbe")
    result = await ca._propose("give checkout 20 minutes to start", _time("20 minutes"))
    assert "never makes up a check" in result["refusal"]["reason"]


@pytest.mark.asyncio
async def test_more_than_an_hour_is_refused(cluster):
    result = await ca._propose("give checkout 2 hours to start", _time("2 hours"))
    assert "more than this agent sets (60 min (3600s))" in result["refusal"]["reason"]


@pytest.mark.asyncio
async def test_exactly_an_hour_is_proposed(cluster):
    _no_startup_probe(cluster)
    pa = (await ca._propose("give checkout 1 hour to start", _time("1 hour")))["pending_action"]
    assert (pa["failure_threshold"], pa["period_seconds"]) == (120, 30)


@pytest.mark.asyncio
async def test_a_time_not_in_the_request_is_refused(cluster):
    result = await ca._propose("give checkout more time to start", _time("30 minutes"))
    assert "does not state one start time" in result["refusal"]["reason"]


@pytest.mark.asyncio
async def test_not_asked_for_is_refused(cluster):
    result = await ca._propose("checkout restarted 5 times", _time("20 minutes"))
    assert result["refusal"]["asked_for"] == "set_startup_probe"


@pytest.mark.asyncio
async def test_the_same_probe_set_by_someone_else_is_refused(cluster):
    _no_startup_probe(cluster)
    pa = await ca._approved(cluster, "give checkout 20 minutes to start", _time("20 minutes"))
    # kubectl sets exactly the approved probe before the approval arrives.
    body = {"spec": {"template": {"spec": {"containers": [{"name": "app", "startupProbe": {
        **LIVENESS, "initialDelaySeconds": 0, "failureThreshold": 40, "periodSeconds": 30}}]}}}}
    await cluster.patch("Deployment", "checkout", "shop", body, "application/strategic-merge-patch+json", False,
                        field_manager="kubectl-patch")
    cluster.patches.clear()
    with pytest.raises(StepRefused, match="done by someone else"):
        await ca._apply(pa, attempt=1)
    # A retry (the worker lost) reads who set it: kubectl, not this agent.
    with pytest.raises(StepRefused, match="done by someone else"):
        await ca._apply(pa, attempt=2)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_the_write_itself_refuses_a_check_that_changed_after_the_re_check(cluster):
    # Between the approval-time re-check and the write, the liveness check changes: the copy is not made.
    from k8s_agent.actions import get

    _no_startup_probe(cluster)
    pa = await ca._approved(cluster, "give checkout 20 minutes to start", _time("20 minutes"))
    spec = get("set_startup_probe")
    _container(cluster)["livenessProbe"]["exec"]["command"] = ["true"]
    with pytest.raises(RuntimeError, match="changed since the proposal, so it was not copied"):
        await spec.build_operations(cluster, spec.patch_action(pa))


def test_the_fingerprint_is_keyed():
    probe = {"exec": {"command": ["redis-cli", "-a", "secret", "ping"]}}
    import hashlib

    plain = hashlib.sha256(json.dumps(views.probe_check(probe), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    original = views._digest_key
    try:
        views._digest_key = lambda: b"a" * 32
        a = views.probe_digest(probe)
        views._digest_key = lambda: b"b" * 32
        b = views.probe_digest(probe)
        views._digest_key = lambda: None
        none = views.probe_digest(probe)
    finally:
        views._digest_key = original
    assert a and b and a != b and plain not in (a, b) and none is None


@pytest.mark.asyncio
async def test_without_the_key_no_check_is_copied(cluster, monkeypatch):
    _no_startup_probe(cluster)
    monkeypatch.setattr(views, "_digest_key", lambda: None)
    result = await ca._propose("give checkout 20 minutes to start", _time("20 minutes"))
    assert "TEMPORAL_PAYLOAD_ENCRYPTION_KEY" in result["refusal"]["reason"] and not cluster.patches


@pytest.mark.parametrize(
    "quote,seconds",
    [("2 minutes (120 seconds)", 120), ("10 minutes", 600), ("600s", 600), ("10分钟", 600), ("2 minutes or 3 minutes", None),
     ("5", None)],
)
def test_stated_duration(quote, seconds):
    assert policy.stated_duration(quote, f"give it {quote} to start") == seconds
