"""Pure logic of the k8s kind: quantities, redaction, views, health rules,
governance, capacity, policy, persistence and rollout assessment."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The k8s kind's worker extra (pip install -e ".[k8s]"); skipped without it, like the CLI tests.
pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from k8s_agent.tools import (  # noqa: E402
    capacity,
    governance,
    health_rules,
    persistence,
    policy,
    quantities,
    redact,
    rollout,
    views,
)

# ---------------------------------------------------------------- quantities


@pytest.mark.parametrize(
    "raw,millis", [("500m", 500), ("1", 1000), ("0.5", 500), ("1.5", 1500), ("7910m", 7910), (2, 2000), ("250000u", 250)]
)
def test_cpu_millis(raw, millis):
    assert quantities.cpu_millis(raw) == millis


@pytest.mark.parametrize(
    "raw,num",
    [("1Gi", 2**30), ("512Mi", 512 * 2**20), ("1G", 10**9), ("31467032Ki", 31467032 * 1024), ("1000", 1000), ("1e3", 1000)],
)
def test_memory_bytes(raw, num):
    assert quantities.memory_bytes(raw) == num


def test_memory_is_always_formatted_with_a_unit_when_it_has_one():
    assert quantities.format_memory(3 * 2**10) == "3Ki"
    assert quantities.format_memory(1000) == "1000"
    assert quantities.memory_request_problem("512Mi") is None
    assert "suffix 'm'" in quantities.memory_request_problem("512m")
    assert "bytes" in quantities.memory_request_problem("1024")


def test_format_round_trips_and_rejects_garbage():
    assert quantities.format_cpu(2000) == "2"
    assert quantities.format_cpu(1500) == "1500m"
    assert quantities.format_memory(4 * 2**30) == "4Gi"
    assert quantities.format_memory(1536 * 2**20) == "1536Mi"
    with pytest.raises(ValueError):
        quantities.parse_quantity("lots")


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "text",
    [
        f"password={fx.CANARY}",
        f'"apiKey": "{fx.CANARY}"',
        f"DB_PASSWORD: {fx.CANARY}",
        f"aws_secret_access_key = {fx.CANARY}",
        f"Authorization: Bearer {fx.CANARY}",
        f"curl https://user:{fx.CANARY}@example.invalid/x",
        f"GET /download?token={fx.CANARY}&x=1",
        f"postgres://svc:{fx.CANARY}@db:5432/app",
        f"client_secret={fx.CANARY}",
    ],
)
def test_redact_text_scrubs_secret_shaped_values(text):
    assert fx.CANARY not in redact.redact_text(text)


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + "A" * 36,
        "github_pat_" + "B" * 40,
        "sk-ant-" + "c" * 30,
        "sk-proj-" + "d" * 30,
        "xoxb-" + "1" * 20,
        "AKIA" + "E" * 16,
        "AIza" + "F" * 35,
        "dckr_pat_" + "g" * 20,
        "glpat-" + "h" * 20,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlc2ln",
    ],
)
def test_redact_text_scrubs_known_token_formats(token):
    assert token not in redact.redact_text(f"error using {token} in request")


def test_redact_text_scrubs_pem_blocks():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in redact.redact_text(f"key:\n{pem}\n")


def test_redact_text_scrubs_half_a_pem_block_cut_by_the_log_tail():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nBKcwggSjAgEAAoIBAQDf3xq9Yb1ZyQ2c\n"
    head_only = redact.redact_text("starting\n-----BEGIN PRIVATE KEY-----\n" + body)
    assert "MIIEvQ" not in head_only and head_only.startswith("starting\n")
    tail_only = redact.redact_text("starting\n" + body + "-----END PRIVATE KEY-----\nready")
    assert "MIIEvQ" not in tail_only and "BKcwgg" not in tail_only
    assert tail_only.startswith("starting\n") and tail_only.endswith("ready")


def test_redact_text_takes_a_quoted_secret_whole():
    out = redact.redact_text('config {"password": "correct horse battery", "user": "app"}')
    assert "horse" not in out and "battery" not in out and '"user": "app"' in out
    assert redact.redact_text(out) == out


def test_redact_obj_replaces_values_under_secret_named_keys():
    out = redact.redact_obj({"db_password": "x", "nested": [{"apiKey": 12345}], "replicas": 3})
    assert out == {"db_password": redact.REDACTED, "nested": [{"apiKey": redact.REDACTED}], "replicas": 3}


def test_strip_url_credentials():
    assert redact.strip_url_credentials(f"https://bot:{fx.CANARY}@git.example.invalid/a/b.git?token={fx.CANARY}#x") == (
        "https://git.example.invalid/a/b.git"
    )
    assert redact.strip_url_credentials("ssh://git@github.com/org/repo") == "ssh://git@github.com/org/repo"


# ---------------------------------------------------------------- views never carry secrets


def test_deployment_view_drops_env_args_and_last_applied():
    view = views.deployment_view(fx.deployment())
    assert fx.CANARY not in repr(view)
    assert view["template"]["containers"][0]["resources"]["limits"] == {"cpu": "1", "memory": "2Gi"}
    assert "kubectl.kubernetes.io/last-applied-configuration" not in view["annotations"]


def test_pod_view_drops_env():
    assert fx.CANARY not in repr(views.pod_view(fx.pod("p1")))


def test_scaledobject_without_kind_targets_a_deployment():
    view = views.scaledobject_view(fx.scaledobject("so", "worker"))
    assert view["target"] == {"kind": "Deployment", "name": "worker"}


# ---------------------------------------------------------------- health rules


def test_crashloop_reports_previous_termination_reason():
    raw = fx.pod(
        "p1",
        containers=[
            {
                "name": "app",
                "ready": False,
                "restartCount": 7,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }
        ],
    )
    [finding] = health_rules.pod_findings(views.pod_view(raw))
    assert finding["severity"] == "critical"
    assert finding["reason"] == "CrashLoopBackOff (OOMKilled)"
    assert "exit code 137" in finding["evidence"]


@pytest.mark.parametrize("restarts,severity,reason", [(3, "critical", "CrashLoopBackOff (Error)"), (0, "warning", "Error")])
def test_terminated_between_restarts_is_still_a_crash_loop(restarts, severity, reason):
    raw = fx.pod(
        "p1b",
        containers=[
            {
                "name": "app",
                "ready": False,
                "restartCount": restarts,
                "state": {"terminated": {"reason": "Error", "exitCode": 1}},
                "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
            }
        ],
    )
    [finding] = health_rules.pod_findings(views.pod_view(raw))
    assert (finding["severity"], finding["reason"]) == (severity, reason)
    assert "exit code 1" in finding["evidence"]


def test_unschedulable_pending_pod():
    raw = fx.pod(
        "p2",
        phase="Pending",
        node=None,
        containers=[],
        conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/3 nodes: Insufficient cpu"}],
    )
    [finding] = health_rules.pod_findings(views.pod_view(raw))
    assert finding["reason"] == "Unschedulable" and "Insufficient cpu" in finding["evidence"]


def test_image_pull_and_restarts():
    raw = fx.pod(
        "p3",
        containers=[{"name": "app", "ready": False, "restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff"}}}],
    )
    assert health_rules.pod_findings(views.pod_view(raw))[0]["reason"] == "ImagePullBackOff"
    raw = fx.pod(
        "p4",
        containers=[
            {
                "name": "app",
                "ready": True,
                "restartCount": 4,
                "state": {"running": {}},
                "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
            }
        ],
    )
    assert health_rules.pod_findings(views.pod_view(raw))[0]["reason"] == "FrequentRestarts (Error)"


def test_deployment_findings_and_usage():
    dep = views.deployment_view(
        fx.deployment(status={"observedGeneration": 7, "availableReplicas": 1, "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}]})
    )
    reasons = {f["reason"] for f in health_rules.deployment_findings(dep)}
    assert reasons == {"ProgressDeadlineExceeded", "ReplicasUnavailable"}

    pod = views.pod_view(fx.pod("p5", limits={"memory": "1Gi"}))
    findings = health_rules.usage_findings([pod], {"shop/p5": {"app": {"memory": "950Mi"}}})
    assert findings[0]["reason"] == "NearMemoryLimit"


# ---------------------------------------------------------------- governance


def test_find_autoscaler_prefers_scaledobject_and_ignores_keda_hpa():
    sos = [views.scaledobject_view(fx.scaledobject("so-worker", "worker"))]
    hpas = [views.hpa_view(fx.hpa("keda-hpa-so-worker", "worker", owned_by_so=True))]
    assert governance.find_autoscaler("worker", sos, hpas)["kind"] == "ScaledObject"
    inferred = governance.find_autoscaler("worker", [], hpas)
    assert inferred["kind"] == "ScaledObject" and inferred["name"] is None

    plain = [views.hpa_view(fx.hpa("web-hpa", "web"))]
    assert governance.find_autoscaler("web", [], plain)["kind"] == "HorizontalPodAutoscaler"
    assert governance.find_autoscaler("other", sos, plain) is None


def test_classify_ownership():
    assert governance.classify_ownership(fx.HELM_LABELS)["class"] == "helm"
    assert governance.classify_ownership({"kustomize.toolkit.fluxcd.io/name": "apps"})["class"] == "kustomization"
    assert governance.classify_ownership({})["class"] == "unowned"


def test_upgrade_activity_counts_last_24h():
    now = datetime.now(timezone.utc)
    history = [{"last_deployed": (now - timedelta(hours=h)).isoformat()} for h in (1, 5, 30)]
    activity = governance.upgrade_activity(history, now)
    assert activity["upgrades_last_24h"] == 2


# ---------------------------------------------------------------- capacity


def _template(**kwargs):
    return views.pod_template_view(fx.deployment(**kwargs)["spec"]["template"])


def test_node_candidate_filters_selector_taints_and_readiness():
    template = _template(node_selector={"pool": "general"})
    good = views.node_view(fx.node("a", labels={"pool": "general"}))
    wrong_pool = views.node_view(fx.node("b", labels={"pool": "gpu"}))
    tainted = views.node_view(fx.node("c", labels={"pool": "general"}, taints=[{"key": "role", "value": "x", "effect": "NoSchedule"}]))
    not_ready = views.node_view(fx.node("d", labels={"pool": "general"}, ready=False))
    assert capacity.node_is_candidate(good, template)
    assert not any(capacity.node_is_candidate(n, template) for n in (wrong_pool, tainted, not_ready))


def test_resource_cap_subtracts_daemonsets_and_applies_headroom():
    nodes = [views.node_view(fx.node("a", cpu="7910m", memory="30Gi"))]
    pods = [views.pod_view(fx.pod("ds-1", node="a", owner_kind="DaemonSet", requests={"cpu": "910m", "memory": "2Gi"}))]
    cap = capacity.resource_cap(nodes, pods, _template(), headroom=0.2)
    assert cap["cpu"]["cap"] == 5600  # (7910 - 910) * 0.8
    assert cap["memory"]["cap"] == quantities.floor_memory_to_mi(int(28 * 2**30 * 0.8))
    assert capacity.resource_cap([], pods, _template()) is None


def _app_with_sidecar():
    containers = [
        {"name": "app", "image": "a", "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}}},
        {"name": "proxy", "image": "p", "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}}},
    ]
    return views.pod_template_view(fx.deployment(containers=containers)["spec"]["template"])


def test_resource_cap_leaves_room_for_the_pods_other_containers():
    nodes = [views.node_view(fx.node("a", cpu="8", memory="10Gi"))]
    cap = capacity.resource_cap(nodes, [], _app_with_sidecar(), headroom=0.2, container="app")
    assert cap["cpu"]["cap"] == 5900  # 8000 * 0.8 - 500
    assert cap["memory"]["cap"] == quantities.floor_memory_to_mi(int(10 * 2**30 * 0.8) - 2**30)
    assert cap["memory"]["other_containers_requests"] == "1Gi"


def test_resource_cap_takes_both_resources_from_one_node():
    cpu_heavy = views.node_view(fx.node("cpu-heavy", cpu="32", memory="8Gi"))
    memory_heavy = views.node_view(fx.node("memory-heavy", cpu="4", memory="64Gi"))
    cap = capacity.resource_cap([cpu_heavy, memory_heavy], [], _app_with_sidecar(), 0.2, "app", {"memory": 32 * 2**30})
    assert cap["cpu"]["node"] == cap["memory"]["node"] == "memory-heavy"
    assert cap["cpu"]["cap"] == 2700  # 4000 * 0.8 - 500, not the 32-CPU node's


def test_a_pod_larger_than_every_node_is_a_high_risk():
    nodes = [views.node_view(fx.node("a", cpu="2", memory="4Gi"))]
    est = capacity.estimate_fit(nodes, [], _template(), {"cpu": 500, "memory": 8 * 2**30}, 1)
    assert est["unplaced"] == 1 and est["nodes_needed"] is None and est["exceeds_every_node"]
    risks = capacity.ca_risks(capacity.parse_ca_status(fx.CA_STATUS_YAML), est["nodes_needed"], est["exceeds_every_node"])
    assert risks[0]["level"] == "high" and "larger instance type" in risks[0]["message"]
    assert not capacity.estimate_fit(nodes, [], _template(), {"cpu": 500, "memory": 2**30}, 1)["exceeds_every_node"]


def test_estimate_fit_counts_unplaced_pods():
    nodes = [views.node_view(fx.node("a", cpu="2", memory="4Gi"))]
    pods = [views.pod_view(fx.pod("x", node="a", requests={"cpu": "1", "memory": "1Gi"}))]
    est = capacity.estimate_fit(nodes, pods, _template(), {"cpu": 500, "memory": 2**30}, 4)
    assert est["fit_now"] == 2 and est["unplaced"] == 2 and est["nodes_needed"] == 1


def test_ca_status_parsing_and_risks():
    ca = capacity.parse_ca_status(fx.CA_STATUS_YAML)
    assert ca["available"] and ca["node_groups"][0]["max_size"] == 50
    assert capacity.ca_risks(ca, 2)[0]["level"] == "info"
    backoff = capacity.parse_ca_status(fx.CA_STATUS_BACKOFF_YAML)
    risk = capacity.ca_risks(backoff, 1)[0]
    assert risk["level"] == "medium" and "VcpuLimitExceeded" in risk["message"]
    assert not capacity.parse_ca_status("free text status").get("available")


def test_classify_pending_precedence():
    pending = [{"name": "p1"}]
    ok_ca = capacity.parse_ca_status(fx.CA_STATUS_YAML)
    scaling = [views.event_view(fx.event("TriggeredScaleUp", "p1", "pod triggered scale-up", type_="Normal"))]
    assert capacity.classify_pending(pending, scaling, ok_ca)["cause"] == "scaling_up"
    cannot = [views.event_view(fx.event("NotTriggerScaleUp", "p1", "max node group size reached"))]
    assert capacity.classify_pending(pending, cannot, ok_ca)["cause"] == "autoscaler_cannot_help"
    failed = capacity.classify_pending(pending, scaling, capacity.parse_ca_status(fx.CA_STATUS_BACKOFF_YAML))
    assert failed["cause"] == "node_provisioning_failed" and failed["stop_waiting"]
    assert capacity.classify_pending(pending, [], ok_ca)["cause"] == "failed_scheduling"


def test_classify_pending_ignores_another_workloads_provisioning_failures():
    pending = [{"name": "p1"}]
    backoff = capacity.parse_ca_status(fx.CA_STATUS_BACKOFF_YAML)
    elsewhere = [views.event_view(fx.event("FailedScaleUp", "gpu-job-1", "quota exceeded"))]
    for events, ca in (([], backoff), (elsewhere, capacity.parse_ca_status(fx.CA_STATUS_YAML))):
        verdict = capacity.classify_pending(pending, events, ca)
        assert verdict["cause"] == "failed_scheduling" and not verdict["stop_waiting"]
    own = [views.event_view(fx.event("FailedScaleUp", "p1", "quota exceeded"))]
    assert capacity.classify_pending(pending, own, capacity.parse_ca_status(fx.CA_STATUS_YAML))["cause"] == "node_provisioning_failed"


# ---------------------------------------------------------------- policy


def test_quote_must_be_verbatim_and_numeric():
    prompt = "Please scale  the Checkout service to 5 replicas"
    assert policy.quote_in_prompt("to 5 replicas", prompt)
    assert policy.quote_in_prompt("the checkout service to 5", prompt)
    assert not policy.quote_in_prompt("to 6 replicas", prompt)
    assert not policy.quote_in_prompt("checkout service", prompt)


def test_redaction_is_idempotent():
    once = redact.redact_text(f"connecting with password={fx.CANARY} now")
    assert redact.redact_text(once) == once == "connecting with password=[REDACTED] now"
    assert redact.redact_obj(redact.redact_obj({"log": once})) == {"log": once}


def test_own_names_do_not_count_as_naming_the_autoscaler():
    assert not policy.mentions_autoscaler("let e2e-hpa go up to 10", ["e2e-hpa", "shop"])
    assert not policy.mentions_autoscaler("scale queue-keda-worker to 4", ["queue-keda-worker"])
    assert policy.mentions_autoscaler("set the HPA max of e2e-hpa to 10", ["e2e-hpa"])


def test_mentions_autoscaler():
    assert policy.mentions_autoscaler("set the KEDA max to 10")
    assert policy.mentions_autoscaler("raise the HPA minimum")
    assert not policy.mentions_autoscaler("scale checkout to 5")


# ---------------------------------------------------------------- persistence


def test_persistence_hint_comes_only_from_chain_fields():
    hr = persistence.helmrelease_summary(fx.helmrelease(), {"upgrades_last_24h": 2})
    chain = {
        "ownership": {"class": "helm"},
        "helmrelease": hr,
        "kustomization": persistence.kustomization_summary(fx.kustomization()),
        "source": persistence.source_summary(fx.gitrepository(), "GitRepository"),
        "broken_at": None,
    }
    hint = persistence.build_hint(chain, [{"object": "Deployment shop/checkout", "field": "spec.replicas", "from": 3, "to": 5}])
    assert hint["available"]
    assert hint["source"]["url"] == "https://git.example.invalid/acme/fleet.git"
    assert hint["as_of_revision"] == "main@sha1:0123456789abcdef"
    assert hint["directory"] == "./clusters/example/apps"
    sources = hint["helmrelease"]["values_sources"]
    assert sources[-1] == {"kind": "inline", "name": "spec.values", "values_key": None, "target_path": None, "optional": False}
    assert "overrides all of them" in hint["helmrelease"]["values_precedence"]
    assert hint["git"] == {
        "host": "git.example.invalid",
        "repo_slug": "acme/fleet",
        "https_url": "https://git.example.invalid/acme/fleet",
        "branch": "main",
        "commit": "0123456789abcdef",
    }
    assert hint["ready_to_persist"] is False and "approved" in hint["readiness_reason"]
    assert hint["changes"] == [{"object": "Deployment shop/checkout", "field": "spec.replicas", "from": "3", "to": "5"}]
    assert fx.CANARY not in repr(hint)


def test_changes_survive_the_protobuf_round_trip_as_manifest_values():
    changes = [
        {"object": "d", "field": "spec.replicas", "from": 1.0, "to": 2.0},
        {"object": "d", "field": "resources.limits.memory", "from": None, "to": "6Gi"},
    ]
    assert persistence.manifest_changes(changes) == [
        {"object": "d", "field": "spec.replicas", "from": "1", "to": "2"},
        {"object": "d", "field": "resources.limits.memory", "from": None, "to": "6Gi"},
    ]


def test_values_sources_keep_order_and_target_path():
    raw = fx.helmrelease(inline_values=False)
    raw["spec"]["valuesFrom"] = [
        {"kind": "ConfigMap", "name": "values", "valuesKey": "aw.yaml"},
        {"kind": "Secret", "name": "creds", "valuesKey": "token", "targetPath": "auth.token", "optional": True},
    ]
    hr = persistence.helmrelease_summary(raw)
    assert [s["values_key"] for s in hr["values_sources"]] == ["aw.yaml", "token"]
    assert hr["values_sources"][1]["target_path"] == "auth.token" and hr["values_sources"][1]["optional"]
    assert hr["chart_source"] == {"kind": "HelmRepository", "name": "charts", "namespace": "flux-apps"}


@pytest.mark.parametrize(
    "url,slug,host",
    [
        ("ssh://git@github.com/insightfinder/test-fluxcd", "insightfinder/test-fluxcd", "github.com"),
        ("https://github.com/acme/fleet.git", "acme/fleet", "github.com"),
        ("ssh://git@gitlab.example.invalid:2222/group/sub/fleet.git", "group/sub/fleet", "gitlab.example.invalid"),
        ("git@github.com:acme/fleet.git", "acme/fleet", "github.com"),
    ],
)
def test_git_coordinates_from_flux_urls(url, slug, host):
    source = {"kind": "GitRepository", "url": url, "branch": "main"}
    coords = persistence.git_coordinates(source, "main@sha1:" + "a" * 40)
    assert coords["repo_slug"] == slug and coords["host"] == host
    assert coords["https_url"] == f"https://{host}/{slug}"
    assert coords["commit"] == "a" * 40


def test_git_coordinates_only_for_git_sources():
    assert persistence.git_coordinates({"kind": "OCIRepository", "url": "oci://r.example.invalid/x"}, None) is None
    assert persistence.git_coordinates(None, None) is None
    legacy = persistence.git_coordinates({"kind": "GitRepository", "url": "https://h/a/b"}, "main/" + "b" * 40)
    assert legacy["commit"] == "b" * 40 and legacy["branch"] is None


@pytest.mark.parametrize(
    "outcome,ready",
    [(None, False), ("succeeded", True), ("in_progress", False), ("unschedulable", False), ("rollout_failed", False)],
)
def test_ready_to_persist_follows_the_outcome(outcome, ready):
    assert persistence.readiness(outcome)["ready_to_persist"] is ready


def test_instruction_names_the_chart_source():
    hr = persistence.helmrelease_summary(fx.helmrelease())
    hr["chart_source"].update(persistence.chart_source_details(fx.helmrepository()))
    chain = {"ownership": {"class": "helm"}, "helmrelease": hr, "kustomization": None, "source": None}
    hint = persistence.build_hint(chain, [])
    assert hr["chart_source"]["url"] == "https://charts.example.invalid/stable"
    assert "chart source: https://charts.example.invalid/stable" in hint["instruction"]
    assert "last source that sets it" in hint["instruction"]
    assert fx.CANARY not in repr(hint)


def test_persistence_hint_for_unowned_and_broken_chains():
    assert not persistence.build_hint({"ownership": {"class": "unowned"}}, [])["available"]
    broken = persistence.build_hint({"ownership": {"class": "helm"}, "broken_at": "HelmRelease x not found"}, [])
    assert not broken["available"] and "x not found" in broken["note"]


# ---------------------------------------------------------------- rollout assessment


def _dep_view(**kwargs):
    return views.deployment_view(fx.deployment(**kwargs))


def test_rollout_success_requires_full_rollout():
    dep = _dep_view(replicas=5)
    assert rollout.assess(dep, {"kind": "replicas", "replicas": 5}, [], [], {}, None)["status"] == "succeeded"
    midway = _dep_view(
        replicas=5,
        status={"observedGeneration": 7, "replicas": 5, "readyReplicas": 3, "availableReplicas": 3, "updatedReplicas": 5},
    )
    assert not rollout.assess(midway, {"kind": "replicas", "replicas": 5}, [], [], {}, None)["done"]


def test_rollout_stops_on_failing_new_pods_and_deadline():
    applied = datetime.now(timezone.utc) - timedelta(minutes=5)
    crashing = views.pod_view(
        fx.pod(
            "new",
            created=fx.iso(1),
            containers=[{"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}],
        )
    )
    verdict = rollout.assess(_dep_view(), {"kind": "replicas", "replicas": 3}, [crashing], [], {}, applied)
    assert verdict["status"] == "new_pods_failing"
    deadline = _dep_view(status={"observedGeneration": 7, "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}]})
    assert rollout.assess(deadline, {"kind": "replicas", "replicas": 3}, [], [], {}, None)["status"] == "rollout_failed"


def test_rollout_ignores_a_deadline_left_over_from_before_the_change():
    applied = datetime.now(timezone.utc) - timedelta(minutes=1)
    stuck = {"type": "Progressing", "reason": "ProgressDeadlineExceeded", "lastUpdateTime": fx.iso(30)}
    target = {"kind": "resources", "container": "app", "resources": {"limits": {"memory": "3Gi"}}}
    not_observed = _dep_view(generation=8, status={"observedGeneration": 7, "conditions": [stuck]})
    assert not rollout.assess(not_observed, target, [], [], {}, applied)["done"]
    observed_but_older = _dep_view(generation=8, status={"observedGeneration": 8, "conditions": [stuck]})
    assert not rollout.assess(observed_but_older, target, [], [], {}, applied)["done"]
    after = _dep_view(generation=8, status={"observedGeneration": 8, "conditions": [{**stuck, "lastUpdateTime": fx.iso(0)}]})
    assert rollout.assess(after, target, [], [], {}, applied)["status"] == "rollout_failed"


def test_rollout_classifies_unschedulable_pods():
    applied = datetime.now(timezone.utc) - timedelta(minutes=5)
    pending = views.pod_view(
        fx.pod("new", created=fx.iso(1), phase="Pending", containers=[], conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}])
    )
    dep = _dep_view(status={"observedGeneration": 7, "replicas": 4, "readyReplicas": 3, "availableReplicas": 3, "updatedReplicas": 4})
    target = {"kind": "replicas", "replicas": 4}
    backoff = capacity.parse_ca_status(fx.CA_STATUS_BACKOFF_YAML)
    declined = [views.event_view(fx.event("NotTriggerScaleUp", "new", "node group in backoff after failed scale-up"))]
    verdict = rollout.assess(dep, target, [pending], declined, backoff, applied)
    assert verdict["status"] == "unschedulable" and verdict["scheduling"]["cause"] == "node_provisioning_failed"
    scaling = [views.event_view(fx.event("TriggeredScaleUp", "new", "scale-up", type_="Normal"))]
    waiting = rollout.assess(dep, target, [pending], scaling, capacity.parse_ca_status(fx.CA_STATUS_YAML), applied)
    assert not waiting["done"] and waiting["scheduling"]["cause"] == "scaling_up"


def test_rollout_autoscaler_change_waits_for_a_lowered_max():
    target = {"kind": "autoscaler", "min": None, "max": 3}
    above = _dep_view(replicas=8, status={"observedGeneration": 7, "replicas": 8, "readyReplicas": 8, "availableReplicas": 8, "updatedReplicas": 8})
    assert not rollout.assess(above, target, [], [], {}, None)["done"]
    within = _dep_view(replicas=3, status={"observedGeneration": 7, "replicas": 3, "readyReplicas": 3, "availableReplicas": 3, "updatedReplicas": 3})
    assert rollout.assess(within, target, [], [], {}, None)["status"] == "succeeded"


def test_quote_must_state_the_extracted_value():
    prompt = "scale web to 3 replicas, memory limit 2 GiB, cpu 0.5 cores"
    assert policy.quote_in_prompt("3 replicas", prompt, 3)
    assert not policy.quote_in_prompt("3 replicas", prompt, 2024)
    assert policy.quote_in_prompt("2 GiB", prompt, "2Gi", "memory")
    assert not policy.quote_in_prompt("2 GiB", prompt, "20Gi", "memory")
    assert policy.quote_in_prompt("0.5 cores", prompt, "500m", "cpu")
    assert not policy.quote_in_prompt("4 replicas", prompt, 4)  # not in the prompt


def test_rollout_resources_checks_new_pod_values():
    dep = _dep_view()
    target = {"kind": "resources", "container": "app", "resources": {"limits": {"memory": "3Gi"}}}
    old = views.pod_view(fx.pod("old", limits={"memory": "2Gi"}))
    assert not rollout.assess(dep, target, [old], [], {}, None)["done"]
    new = views.pod_view(fx.pod("new", limits={"memory": "3Gi"}))
    assert rollout.assess(dep, target, [new], [], {}, None)["status"] == "succeeded"


# ---------------------------------------------------------------- rollout stability window


def _new_pod(restarts, state):
    return views.pod_view(
        fx.pod("checkout-new", created=fx.iso(0), containers=[{"name": "app", "ready": True, "restartCount": restarts, "state": state}])
    )


@pytest.mark.asyncio
async def test_a_rollout_that_crashes_right_after_becoming_ready_is_not_a_success(monkeypatch):
    """Found live: an OOM-killed container is ready for a few seconds after each start."""
    dep = views.deployment_view(fx.deployment(replicas=1))
    looks = iter([
        [_new_pod(0, {"running": {}})],
        [_new_pod(1, {"terminated": {"reason": "OOMKilled", "exitCode": 137}})],
    ])

    async def get_deployment(namespace, name):
        return dep

    async def list_pods(namespace, selector=None):
        return next(looks)

    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(rollout.read, "get_deployment", get_deployment)
    monkeypatch.setattr(rollout.read, "list_pods", list_pods)
    monkeypatch.setattr(rollout.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rollout, "POLL_SECONDS", 0)
    applied_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    verdict = await rollout.wait_rollout("shop", "checkout", {"kind": "replicas", "replicas": 1}, applied_at, 600)
    assert verdict["status"] == "new_pods_failing"
    assert verdict["failing"][0]["last_termination"] == "OOMKilled (exit code 137)"


@pytest.mark.asyncio
async def test_a_rollout_that_stays_up_through_the_window_succeeds(monkeypatch):
    dep = views.deployment_view(fx.deployment(replicas=1))

    async def get_deployment(namespace, name):
        return dep

    async def list_pods(namespace, selector=None):
        return [_new_pod(0, {"running": {}})]

    clock = iter(range(0, 10_000, 20))
    monkeypatch.setattr(rollout.read, "get_deployment", get_deployment)
    monkeypatch.setattr(rollout.read, "list_pods", list_pods)
    monkeypatch.setattr(rollout.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rollout, "POLL_SECONDS", 0)
    applied_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    verdict = await rollout.wait_rollout("shop", "checkout", {"kind": "replicas", "replicas": 1}, applied_at, 600)
    assert verdict["status"] == "succeeded" and verdict["stable_for_seconds"] >= rollout.STABILITY_SECONDS


def test_label_selector_keeps_match_expressions():
    selector = {
        "matchLabels": {"app": "web"},
        "matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["a", "b"]},
            {"key": "canary", "operator": "DoesNotExist"},
            {"key": "zone", "operator": "Exists"},
            {"key": "env", "operator": "NotIn", "values": ["dev"]},
        ],
    }
    assert views.label_selector(selector) == "app=web,tier in (a,b),!canary,zone,env notin (dev)"
    only_expressions = views.deployment_view({"metadata": {"name": "w"}, "spec": {"selector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["w"]}]}}})
    assert only_expressions["selector"] == {} and only_expressions["label_selector"] == "app in (w)"


@pytest.mark.asyncio
async def test_a_change_undone_during_the_rollout_is_reported_as_reverted(monkeypatch):
    applied = views.deployment_view(fx.deployment(replicas=3))
    put_back = views.deployment_view(fx.deployment(replicas=2))  # a GitOps reconcile restores its own value
    looks = iter([applied, put_back])

    async def get_deployment(namespace, name):
        return next(looks)

    async def list_pods(namespace, selector=None):
        return []

    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(rollout.read, "get_deployment", get_deployment)
    monkeypatch.setattr(rollout.read, "list_pods", list_pods)
    monkeypatch.setattr(rollout.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rollout, "POLL_SECONDS", 0)
    applied_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    verdict = await rollout.wait_rollout("shop", "checkout", {"kind": "replicas", "replicas": 3}, applied_at, 600)
    assert verdict["status"] == "reverted" and "spec.replicas is 2 again" in verdict["detail"]


def test_moved_away_compares_resource_quantities_not_strings():
    dep = views.deployment_view(fx.deployment())
    container = dep["template"]["containers"][0]
    container["resources"] = {"limits": {"memory": "1024Mi", "cpu": "0.5"}}
    target = {"kind": "resources", "container": container["name"], "resources": {"limits": {"memory": "1Gi", "cpu": "500m"}}}
    assert rollout.moved_away(dep, target) is None
    container["resources"] = {"limits": {"memory": "512Mi", "cpu": "500m"}}
    assert "limits.memory" in rollout.moved_away(dep, target)
