"""What restarted a container: an OOM kill, a startup or liveness probe, or nothing on record.

The kubelet's wording and the exit codes were taken from test (2026-11-27): a startup-probe kill of a process
that ignores SIGTERM ended `Error` / 137, a liveness-probe kill of one that exits on SIGTERM ended
`Completed` / 0, and both left a Normal `Killing` event "Container app failed <kind> probe, will be restarted".
"""

import pytest

from k8s_agent.capabilities import check_health
from k8s_agent.tools import health_rules, read, views

from . import k8s_fixtures as fx
from .test_k8s_capabilities import _ctx, _resolved, cluster  # noqa: F401 — the fixture is used by name

STARTUP = {"startupProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 5, "failureThreshold": 3}}
LIVENESS = {"livenessProbe": {"exec": {"command": ["sh", "-c", "check --password=s3cret"]}, "initialDelaySeconds": 5, "periodSeconds": 5, "failureThreshold": 2}}


def _terminated(reason, exit_code, ran_seconds=17):
    return {"terminated": {"reason": reason, "exitCode": exit_code, "startedAt": "2026-11-27T06:38:31Z",
                           "finishedAt": f"2026-11-27T06:38:{31 + ran_seconds}Z" if ran_seconds < 29 else "2026-11-27T07:38:31Z"}}


def _pod(name, last, *, state=None, restarts=4, probes=None):
    container = {
        "name": "app",
        "ready": False,
        "restartCount": restarts,
        "state": state or {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
        "lastState": last,
    }
    raw = fx.pod(name, containers=[container], probes=probes)
    raw["spec"]["terminationGracePeriodSeconds"] = 5
    return views.pod_view(raw)


def _kill(pod, probe, container="app", count=3):
    return {"namespace": "shop", "pod": pod, "container": container, "probe": probe, "count": count, "last_seen": fx.iso(1)}


def test_startup_probe_kill_is_named_with_its_allowance():
    pod = _pod("p1", _terminated("Error", 137), probes=STARTUP)
    [finding] = health_rules.pod_findings(pod, [_kill("p1", "startup")])
    assert finding["reason"] == "CrashLoopBackOff (killed by startup probe)"
    assert finding["killed_by"] == "startup_probe" and finding["probe_kills"] == 3
    assert finding["last_run_seconds"] == 17
    assert finding["probe"]["allows_seconds"] == 15 and finding["probe"]["handler"] == "tcpSocket"
    assert "check_digest" not in finding["probe"]
    assert "raise startupProbe.failureThreshold" in finding["diagnosis"]
    # A Deployment's pod: the agent can propose the fix itself.
    assert "give deployment <name> 10 minutes to start" in finding["diagnosis"]
    assert "exit code 137" in finding["evidence"]


def test_liveness_kill_while_starting_suggests_a_startup_probe():
    # Exits 0 on SIGTERM: the container shows `Completed` and runs again, below the FrequentRestarts threshold.
    pod = _pod("p2", _terminated("Completed", 0, ran_seconds=12), state={"running": {"startedAt": fx.iso(0)}}, restarts=1, probes=LIVENESS)
    finding, not_ready = health_rules.pod_findings(pod, [_kill("p2", "liveness")])
    assert not_ready["reason"] == "NotReady"
    assert finding["reason"] == "Restarted (killed by liveness probe)" and finding["severity"] == "warning"
    assert finding["during_startup"] is True
    assert "add a startupProbe with the same check as the liveness probe" in finding["diagnosis"]
    # The check itself is never copied: an exec command can carry a password.
    assert "s3cret" not in str(finding) and finding["probe"]["handler"] == "exec"


def test_liveness_kill_after_a_long_run_is_a_hang_not_a_slow_start():
    pod = _pod("p3", _terminated("Error", 143, ran_seconds=3600), probes=LIVENESS)
    [finding] = health_rules.pod_findings(pod, [_kill("p3", "liveness")])
    assert finding["during_startup"] is False
    assert "stopped passing its liveness check while running" in finding["diagnosis"]


def test_liveness_kill_with_a_startup_probe_is_not_a_slow_start():
    pod = _pod("p4", _terminated("Error", 137, ran_seconds=12), probes={**STARTUP, **LIVENESS})
    [finding] = health_rules.pod_findings(pod, [_kill("p4", "liveness")])
    assert finding["during_startup"] is False


def test_exit_137_without_a_recorded_kill_is_not_called_an_oom_kill():
    pod = _pod("p5", _terminated("Error", 137), probes=STARTUP)
    [finding] = health_rules.pod_findings(pod, [])
    assert finding["reason"] == "CrashLoopBackOff (Error)"
    assert finding["killed_by"] == "not_recorded"
    assert "not recorded as OOMKilled" in finding["diagnosis"] and "events expire" not in finding["reason"]


def test_oom_kill_stays_an_oom_kill_even_beside_a_probe_kill_event():
    pod = _pod("p6", _terminated("OOMKilled", 137), probes=STARTUP)
    [finding] = health_rules.pod_findings(pod, [_kill("p6", "startup")])
    assert finding["reason"] == "CrashLoopBackOff (OOMKilled)" and finding["killed_by"] == "oom"


def test_a_kill_of_another_pod_or_container_does_not_count():
    pod = _pod("p7", _terminated("Error", 137), probes=STARTUP)
    [finding] = health_rules.pod_findings(pod, [_kill("p8", "startup"), _kill("p7", "startup", container="sidecar")])
    assert finding["killed_by"] == "not_recorded"


def test_an_ordinary_crash_has_no_kill_cause():
    pod = _pod("p9", _terminated("Error", 1))
    [finding] = health_rules.pod_findings(pod, [])
    assert finding["reason"] == "CrashLoopBackOff (Error)" and "killed_by" not in finding


def test_probe_allowance():
    assert health_rules.probe_allowance({"initial_delay_seconds": 10, "failure_threshold": 3, "period_seconds": 30}) == 100


@pytest.mark.asyncio
async def test_list_probe_kills_reads_only_probe_kills(cluster):  # noqa: F811
    cluster.objects["Event"] = [
        fx.event("Killing", "p1", "Container app failed startup probe, will be restarted", type_="Normal", count=4, field_path="spec.containers{app}"),
        fx.event("Killing", "p2", "Container app failed liveness probe, will be restarted", type_="Normal", minutes_ago=5),
        # An ordinary stop has the same reason.
        fx.event("Killing", "p3", "Stopping container app", type_="Normal"),
        # Expired from the window.
        fx.event("Killing", "p4", "Container app failed startup probe, will be restarted", type_="Normal", minutes_ago=90),
        # A failing readiness probe does not restart anything.
        fx.event("Unhealthy", "p5", "Readiness probe failed: connection refused"),
    ]
    kills = await read.list_probe_kills("shop")
    assert [(k["pod"], k["container"], k["probe"], k["count"]) for k in kills] == [("p1", "app", "startup", 4), ("p2", "app", "liveness", 1)]


@pytest.mark.asyncio
async def test_check_health_names_a_probe_kill_and_reads_its_previous_log(cluster):  # noqa: F811
    name = f"checkout-{fx.TEMPLATE_HASH}-slow1"
    raw = fx.pod(
        name,
        probes=STARTUP,
        containers=[{"name": "app", "ready": False, "restartCount": 1, "state": {"running": {"startedAt": fx.iso(0)}},
                     "lastState": _terminated("Error", 137)}],
    )
    cluster.objects["Pod"] = [raw]
    cluster.objects["Event"] = [fx.event("Killing", name, "Container app failed startup probe, will be restarted", type_="Normal")]
    cluster.logs[name] = "Rebuilding message store indices...\n"
    report = (await check_health.run(_ctx("check_health", "check checkout", results=_resolved("checkout"))))["report"]
    finding = next(f for f in report["findings"] if f["name"] == name)
    assert finding["reason"] == "Restarted (killed by startup probe)" and finding["killed_by"] == "startup_probe"
    assert "Rebuilding message store indices" in finding["previous_log_tail"]


@pytest.mark.asyncio
async def test_check_health_states_unreadable_probe_kills_as_a_gap(cluster, monkeypatch):  # noqa: F811
    real = cluster.list

    async def no_killing(kind, namespace=None, label_selector=None, field_selector=None, max_items=5000):
        if kind == "Event" and field_selector == "reason=Killing":
            raise RuntimeError("events are forbidden")
        return await real(kind, namespace, label_selector, field_selector, max_items)

    monkeypatch.setattr(cluster, "list", no_killing)
    monkeypatch.setattr(read, "_retry", lambda call: call())
    report = (await check_health.run(_ctx("check_health", "check checkout", results=_resolved("checkout"))))["report"]
    assert any(g.startswith("probe kills (Killing events) could not be read") for g in report["gaps"])


# ---------------------------------------------------------------- OOM kill: a suggested memory limit


def _oom_pod(name, limit, owner_kind="ReplicaSet"):
    raw = fx.pod(
        name,
        app="checkout",
        owner_kind=owner_kind,
        limits={"memory": limit} if limit else None,
        requests={"cpu": "100m", "memory": "256Mi"},
        containers=[{"name": "app", "ready": False, "restartCount": 5, "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                     "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}],
    )
    return raw


async def _oom_report(cluster, *pods):  # noqa: F811
    cluster.objects["Pod"] = list(pods)
    return (await check_health.run(_ctx("check_health", "check checkout", results=_resolved("checkout"))))["report"]


@pytest.mark.asyncio
async def test_oom_kill_suggests_twice_the_limit_and_the_request(cluster):  # noqa: F811
    name = f"checkout-{fx.TEMPLATE_HASH}-oom01"
    report = await _oom_report(cluster, _oom_pod(name, "512Mi"))
    finding = next(f for f in report["findings"] if f["name"] == name)
    s = finding["suggestion"]
    assert (s["field"], s["from"], s["to"]) == ("limits.memory", "512Mi", "1Gi")
    assert s["basis"] == "2 x the current limit 512Mi" and "first step" in s["note"]
    assert s["request"] == "set the memory limit of container app in deployment checkout in namespace shop to 1Gi"


@pytest.mark.asyncio
async def test_oom_suggestion_is_capped_by_the_largest_node(cluster):  # noqa: F811
    name = f"checkout-{fx.TEMPLATE_HASH}-oom02"
    report = await _oom_report(cluster, _oom_pod(name, "16Gi"))
    s = next(f for f in report["findings"] if f["name"] == name)["suggestion"]
    # 30Gi nodes, 2Gi of DaemonSets, 20% headroom: about 22.4Gi, not 32Gi.
    assert "capped by the largest eligible node" in s["basis"]
    assert 16 * 2**30 < fx_units(s["to"]) < 32 * 2**30 and s["request"].endswith(s["to"])


@pytest.mark.asyncio
async def test_oom_at_the_node_cap_suggests_no_value(cluster):  # noqa: F811
    name = f"checkout-{fx.TEMPLATE_HASH}-oom03"
    report = await _oom_report(cluster, _oom_pod(name, "28Gi"))
    s = next(f for f in report["findings"] if f["name"] == name)["suggestion"]
    assert "to" not in s and "request" not in s and "no eligible node can give" in s["note"]


@pytest.mark.asyncio
async def test_oom_without_a_limit_suggests_a_request_instead(cluster):  # noqa: F811
    name = f"checkout-{fx.TEMPLATE_HASH}-oom04"
    report = await _oom_report(cluster, _oom_pod(name, None))
    s = next(f for f in report["findings"] if f["name"] == name)["suggestion"]
    assert "to" not in s and "no memory limit" in s["note"]


def test_oom_suggestion_for_a_pod_of_no_deployment_has_no_request():
    import asyncio

    from k8s_agent.capabilities.check_health import _oom_suggestions

    pod = views.pod_view(_oom_pod("db-0", "1Gi", owner_kind="StatefulSet"))
    findings = health_rules.pod_findings(pod, [])
    asyncio.run(_oom_suggestions(None, findings, [pod], {}))
    s = findings[0]["suggestion"]
    assert s["to"] == "2Gi" and "request" not in s


def fx_units(value):
    from k8s_agent.tools import quantities

    return quantities.to_units("memory", value)


def _oom_after(ran_seconds):
    import asyncio

    from k8s_agent.capabilities.check_health import _oom_suggestions

    raw = _oom_pod("db-0", "1Gi", owner_kind="StatefulSet")
    raw["status"]["containerStatuses"][0]["lastState"]["terminated"].update(
        {"startedAt": "2026-11-27T00:00:00Z", "finishedAt": f"2026-11-27T{ran_seconds // 3600:02d}:{ran_seconds % 3600 // 60:02d}:00Z"})
    pod = views.pod_view(raw)
    findings = health_rules.pod_findings(pod, [])
    asyncio.run(_oom_suggestions(None, findings, [pod], {}))
    return findings[0]


def test_oom_soon_after_start_is_a_limit_set_too_low():
    finding = _oom_after(120)
    assert finding["last_run_seconds"] == 120
    s = finding["suggestion"]
    assert s["pattern"] == "early" and s["to"] == "2Gi" and "first step" in s["note"]


def test_oom_after_hours_looks_like_a_leak_but_still_has_a_value():
    s = _oom_after(5 * 3600)["suggestion"]
    assert s["pattern"] == "long_run" and s["to"] == "2Gi"
    assert "ran 300 min before being killed" in s["note"] and "only delays the next OOM kill" in s["note"]
