"""Write batch 2 — `set_image`, `pause_rollout` / `resume_rollout`, `suspend_cronjob` / `resume_cronjob` — end
to end against the fake API, plus CronJob resolution and health.

Uses the cluster of test_k8s_change_actions.py (checkout with its ReplicaSets, StatefulSet `db`, DaemonSet
`agent`) and adds a CronJob `report` with one past run's pod.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx, _resolved  # noqa: E402
from test_k8s_change_actions import _apply, _obj, _workload, build_cluster  # noqa: E402

from k8s_agent.capabilities import check_health, find_workload, propose_change  # noqa: E402
from k8s_agent.tools import governance, policy, read, rollout  # noqa: E402

pytestmark = pytest.mark.asyncio
_CRON = _workload("CronJob", "report")


@pytest.fixture
def cluster(monkeypatch):
    fake = build_cluster(monkeypatch)
    fake.objects["CronJob"] = [fx.cronjob("report")]
    fake.objects["Job"] = []
    fake.objects["Pod"].append(fx.job_pod("report-29410560-x7k2p", "report-29410560"))
    return fake


async def _propose(prompt, change, results=None):
    return await propose_change.run(_ctx("propose_change", prompt, results=results or _resolved("checkout"), llm=ScriptedLLM(change)))


async def _approved(cluster, prompt, change, results=None):
    result = await _propose(prompt, change, results)
    assert "pending_action" in result, result
    cluster.patches.clear()
    return fx.a2a_round_trip(result["pending_action"])


# ---------------------------------------------------------------- intent gates and image helpers


@pytest.mark.parametrize(
    "gate,prompt",
    [
        ("asks_set_image", "set the image of checkout to registry.example/checkout:1.3"),
        ("asks_set_image", "把 checkout 的镜像改成 registry.example/checkout:1.3"),
        ("asks_set_image", "upgrade checkout to version 1.3"),
        ("asks_set_image", "bump checkout to registry.example/checkout:1.3"),
        ("asks_pause_rollout", "pause the rollout of checkout"),
        ("asks_pause_rollout", "暂停 checkout 的发布"),
        ("asks_resume_rollout", "resume the rollout of checkout"),
        ("asks_resume_rollout", "取消暂停 checkout"),
        ("asks_suspend_cronjob", "suspend cronjob report"),
        ("asks_suspend_cronjob", "停用 report 定时任务"),
        ("asks_resume_cronjob", "re-enable the report cronjob"),
        ("asks_resume_cronjob", "恢复 report 定时任务"),
    ],
)
def test_the_request_asks_for_it(gate, prompt):
    assert getattr(policy, gate)(prompt)


@pytest.mark.parametrize(
    "gate,prompt",
    [
        ("asks_set_image", "checkout fails to pull registry.example/checkout:1.3, why?"),
        ("asks_set_image", "don't change the image of checkout"),
        ("asks_set_image", "after the upgrade, checkout pods fail to pull registry.example/checkout:1.3"),
        ("asks_set_image", "we tried to upgrade checkout to registry.example/checkout:1.3 yesterday, why did it fail?"),
        ("asks_set_image", "the checkout pods use image registry.example/checkout:1.2"),
        ("asks_set_image", "ImagePullBackOff on registry.example/checkout:1.3"),
        ("asks_pause_rollout", "checkout is paused, why?"),
        ("asks_pause_rollout", "checkout 被暂停了吗"),
        ("asks_pause_rollout", "不要暂停 checkout"),
        ("asks_resume_rollout", "checkout 恢复正常了吗"),
        ("asks_resume_rollout", "checkout 已恢复"),
        ("asks_suspend_cronjob", "report is suspended, why?"),
        ("asks_suspend_cronjob", "report 已停用"),
        ("asks_resume_cronjob", "report 恢复正常了吗"),
    ],
)
def test_the_request_does_not_ask_for_it(gate, prompt):
    assert not getattr(policy, gate)(prompt)


def test_an_image_is_stated_only_as_a_whole_reference():
    assert policy.states_image("set it to nginx:1.25.", "nginx:1.25")
    assert not policy.states_image("set it to app:1.2.3", "app:1.2")
    assert not policy.states_image("set it to evil.io/app:1.2", "app:1.2")
    assert not policy.valid_image("app:1.2 && rm")


def test_a_tag_is_stated_only_as_a_tag():
    assert policy.states_tag("set the tag to 1.3", "1.3") and policy.states_tag("use checkout:1.4", "1.4")
    assert not policy.states_tag("scale to 3 replicas", "3")


def test_split_image_keeps_the_registry_port():
    assert policy.split_image("registry:5000/team/app:1.2@sha256:abc") == ("registry:5000/team/app", "1.2")
    assert policy.split_image("registry:5000/app") == ("registry:5000/app", None)


# ---------------------------------------------------------------- set_image


async def test_set_image_of_a_deployment_is_proposed(cluster):
    prompt = "set the image of checkout to registry.example/checkout:1.3"
    result = await _propose(prompt, _change(action="set_image", image="registry.example/checkout:1.3"))
    pa = result["pending_action"]
    assert pa["action"] == "set_image" and pa["container"] == "app" and pa["image"] == "registry.example/checkout:1.3"
    [dry] = cluster.patches
    assert dry["dry_run"] is True and dry["content_type"] == "application/strategic-merge-patch+json"
    assert dry["body"] == {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "registry.example/checkout:1.3"}]}}}}
    assert pa["risk_tier"] == "medium"
    assert pa["persistence"]["changes"] == [
        {"object": "Deployment shop/checkout container app", "field": "image", "from": "registry.example/checkout:1.2", "to": "registry.example/checkout:1.3"}
    ]
    assert any("ImagePullBackOff" in n for n in pa["notes"])
    assert fx.CANARY not in json.dumps(result)


async def test_a_tag_goes_on_the_current_repository(cluster):
    pa = (await _propose("set checkout to tag 1.4", _change(action="set_image", tag="1.4")))["pending_action"]
    assert pa["image"] == "registry.example/checkout:1.4"


async def test_a_tag_not_written_as_one_is_refused(cluster):
    result = await _propose("upgrade checkout to 1.4", _change(action="set_image", tag="1.4"))
    assert "tag" in result["refusal"]["reason"] and not cluster.patches


async def test_an_image_only_inside_a_longer_one_is_refused(cluster):
    change = _change(action="set_image", image="registry.example/checkout:1.3")
    result = await _propose("set the image of checkout to registry.example/checkout:1.3.1", change)
    assert "does not write the new image" in result["refusal"]["reason"]


async def test_a_container_not_in_the_template_is_refused(cluster):
    change = _change(action="set_image", image="registry.example/checkout:1.3", container="sidecar")
    result = await _propose("set the image of container sidecar in checkout to registry.example/checkout:1.3", change)
    assert result["refusal"]["containers"] == ["app"] and not cluster.patches


async def test_the_same_image_is_refused(cluster):
    change = _change(action="set_image", image="registry.example/checkout:1.2")
    result = await _propose("set the image of checkout to registry.example/checkout:1.2", change)
    assert "already runs" in result["refusal"]["reason"]


async def test_another_repository_is_a_high_risk(cluster):
    change = _change(action="set_image", image="docker.io/someone/checkout:1.3")
    pa = (await _propose("set the image of checkout to docker.io/someone/checkout:1.3", change))["pending_action"]
    assert pa["risk_tier"] == "high" and any("repository changes" in r["message"] for r in pa["risks"])


async def test_an_image_quoted_from_a_finding_is_not_an_image_change(cluster):
    change = _change(action="set_image", image="registry.example/checkout:1.3")
    result = await _propose("checkout pods fail to pull registry.example/checkout:1.3, what is wrong?", change)
    assert result["refusal"]["asked_for"] == "set_image" and not cluster.patches


async def test_set_image_apply_writes_once_and_verifies(cluster):
    pa = await _approved(cluster, "set the image of checkout to registry.example/checkout:1.3", _change(action="set_image", image="registry.example/checkout:1.3"))
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["applied"]["images"] == {"app": "registry.example/checkout:1.3"}
    assert result["persistence"]["changes"][0]["to"] == "registry.example/checkout:1.3"
    cluster.patches.clear()
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and not cluster.patches


async def test_set_image_apply_refuses_after_the_image_changed(cluster):
    pa = await _approved(cluster, "set the image of checkout to registry.example/checkout:1.3", _change(action="set_image", image="registry.example/checkout:1.3"))
    _obj(cluster, "Deployment", "checkout")["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example/checkout:1.2.1"
    with pytest.raises(RuntimeError, match="now runs registry.example/checkout:1.2.1"):
        await _apply(pa)
    assert not cluster.patches


async def test_set_image_of_a_statefulset(cluster):
    prompt = "set the image of statefulset db to registry.example/db:17"
    pa = await _approved(cluster, prompt, _change(action="set_image", image="registry.example/db:17"), _workload("StatefulSet", "db"))
    assert pa["target"] == {"kind": "StatefulSet", "namespace": "shop", "name": "db"} and pa["container"] == "db"
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded"
    [write] = cluster.patches
    assert write["kind"] == "StatefulSet" and write["dry_run"] is False


async def test_a_workload_image_put_back_is_reverted():
    view = {"kind": "StatefulSet", "template": {"containers": [{"name": "db", "image": "registry.example/db:16"}]}}
    assert "runs image registry.example/db:16 again" in rollout.moved_away(view, {"kind": "images", "images": {"db": "registry.example/db:17"}})


# ---------------------------------------------------------------- pause_rollout / resume_rollout


async def test_pause_is_proposed_and_replaces_no_pod(cluster):
    result = await _propose("pause the rollout of checkout", _change(action="pause_rollout"))
    pa = result["pending_action"]
    assert pa["paused"] is True and pa["risk_tier"] == "medium"
    [dry] = cluster.patches
    assert dry["body"] == {"spec": {"paused": True}} and dry["content_type"] == "application/merge-patch+json"
    assert any("Nothing resumes it" in r["message"] for r in pa["risks"])
    assert not any("replaces every pod" in r["message"] for r in pa["risks"])
    assert pa["estimated_seconds_per_pod_to_ready"] is None and pa["persistence"]["needed"] is False


async def test_pausing_a_paused_deployment_is_refused(cluster):
    _obj(cluster, "Deployment", "checkout")["spec"]["paused"] = True
    result = await _propose("pause the rollout of checkout", _change(action="pause_rollout"))
    assert "already paused" in result["refusal"]["reason"]


async def test_resuming_a_deployment_that_is_not_paused_is_refused(cluster):
    result = await _propose("resume the rollout of checkout", _change(action="resume_rollout"))
    assert "not paused" in result["refusal"]["reason"]


async def test_pause_apply_waits_for_the_controller(cluster):
    pa = await _approved(cluster, "pause the rollout of checkout", _change(action="pause_rollout"))
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["verification"]["progressing_reason"] == "DeploymentPaused"
    assert _obj(cluster, "Deployment", "checkout")["spec"]["paused"] is True


async def test_resume_with_a_waiting_template_change_rolls_the_pods(cluster):
    dep = _obj(cluster, "Deployment", "checkout")
    dep["spec"]["paused"] = True
    dep["status"]["updatedReplicas"] = 0  # the template changed while paused (as observed on test)
    pa = await _approved(cluster, "resume the rollout of checkout", _change(action="resume_rollout"))
    assert pa["pending_pods"] == 3 and any("resuming replaces 3 of 3 pods" in r["message"] for r in pa["risks"])
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and dep["spec"]["paused"] is False


async def test_resume_without_a_waiting_change_replaces_no_pod(cluster):
    _obj(cluster, "Deployment", "checkout")["spec"]["paused"] = True
    pa = await _approved(cluster, "unpause checkout", _change(action="resume_rollout"))
    assert pa["pending_pods"] == 0 and any("replaces no pod" in n for n in pa["notes"])
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["verification"]["progressing_reason"] == "NewReplicaSetAvailable"


async def test_pausing_a_timed_out_rollout_is_acknowledged(monkeypatch):
    # The controller keeps ProgressDeadlineExceeded through a pause and never writes DeploymentPaused.
    async def stuck(namespace, name):
        return {"paused": True, "generation": 3, "observed_generation": 3,
                "conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}]}

    monkeypatch.setattr(read, "get_deployment", stuck)
    monkeypatch.setattr(rollout, "STABILITY_SECONDS", 0)
    assert (await rollout.wait_paused("shop", "checkout", True, 30))["status"] == "succeeded"


async def test_a_resume_approved_as_replacing_no_pod_is_refused_after_the_template_changed(cluster):
    dep = _obj(cluster, "Deployment", "checkout")
    dep["spec"]["paused"] = True
    pa = await _approved(cluster, "resume the rollout of checkout", _change(action="resume_rollout"))
    assert pa["pending_pods"] == 0
    dep["status"]["updatedReplicas"] = 0  # image automation changed the template while it stayed paused
    with pytest.raises(RuntimeError, match="resuming would now replace 3 pod"):
        await _apply(pa)
    assert not cluster.patches


def test_every_example_request_passes_its_own_gate():
    """What a refusal tells the user to ask, and what the live script asks, must be proposable."""
    import re

    import k8s_agent.actions as registry
    from test_k8s_live_script import live

    for name, spec in registry.REGISTRY.items():
        for kind in spec.workload_kinds:
            text = spec.not_asked.format(kind=kind, name="insightfinder-dailyreportworker", namespace="insightfinder")
            for example in re.findall(r'"([^"]+)"', text):
                assert spec.accepts(example), (name, example)
    ns = live.NAMESPACE
    for name, prompt in [
        ("suspend_cronjob", f"suspend the cronjob e2e-cron in namespace {ns}"),
        ("resume_cronjob", f"resume the cronjob e2e-cron in namespace {ns}"),
        ("pause_rollout", f"pause the rollout of deployment e2e-plain in namespace {ns}"),
        ("resume_rollout", f"resume the rollout of deployment e2e-plain in namespace {ns}"),
        ("set_image", f"set the image of container app in deployment e2e-plain in namespace {ns} to {live.PAUSE_NEW}"),
        ("restart_rollout", f"restart the statefulset e2e-sts in namespace {ns}"),
    ]:
        assert registry.get(name).accepts(prompt), prompt
    for example in re.findall(r"'([^']+)'", propose_change._example("CronJob", "insightfinder-dailyreportworker", "insightfinder")):
        assert registry.get("suspend_cronjob").accepts(example), example


async def test_a_pause_undone_while_watched_is_reverted(monkeypatch):
    async def unpaused(namespace, name):
        return {"paused": False, "generation": 2, "observed_generation": 2, "conditions": []}

    monkeypatch.setattr(read, "get_deployment", unpaused)
    result = await rollout.wait_paused("shop", "checkout", True, 30)
    assert result["status"] == "reverted"


# ---------------------------------------------------------------- CronJob resolution and health


async def test_find_workload_resolves_a_cronjob_by_name(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", "suspend cronjob report", llm=llm))
    assert result["resolution"] == {"scope": "workload", "kind": "CronJob", "namespace": "shop", "name": "report"}
    assert llm.prompts == []


async def test_find_workload_resolves_a_scheduled_run_pod_to_its_cronjob(cluster):
    result = await find_workload.run(_ctx("find_workload", "why did report-29410560-x7k2p fail?", llm=ScriptedLLM()))
    assert result["resolution"]["kind"] == "CronJob" and result["resolution"]["name"] == "report"


async def test_check_health_of_a_cronjob_looks_at_its_runs_pods(cluster):
    _obj(cluster, "CronJob", "report")["spec"]["suspend"] = True
    result = await check_health.run(_ctx("check_health", "check report", results=_CRON, options={"include_logs": False}))
    report = result["report"]
    assert report["summary"]["pods"] == 1
    assert any(f["kind"] == "CronJob" and f["reason"] == "Suspended" and f["severity"] == "info" for f in report["findings"])


def test_a_cronjob_owns_its_scheduled_runs_only():
    assert governance.is_cronjob_job("report-29410560", "report")
    assert not governance.is_cronjob_job("report-v2-29410560", "report")
    assert not governance.is_cronjob_job("reporter-29410560", "report")
    assert not governance.is_cronjob_job("report-manual-x2k4q", "report")


async def test_a_cronjob_is_offered_only_cronjob_actions(cluster):
    llm = ScriptedLLM(_change(action="restart_rollout"))
    result = await propose_change.run(_ctx("propose_change", "restart report", results=_CRON, llm=llm))
    assert "does not ask for a concrete change" in result["refusal"]["reason"]
    assert '"suspend_cronjob"' in llm.prompts[0] and '"restart_rollout"' not in llm.prompts[0]
    assert '"trigger_cronjob"' not in llm.prompts[0]
    assert "suspend cronjob report" in result["refusal"]["reason"]


# ---------------------------------------------------------------- suspend_cronjob / resume_cronjob


async def test_suspend_is_proposed(cluster):
    result = await _propose("suspend cronjob report", _change(action="suspend_cronjob"), _CRON)
    pa = result["pending_action"]
    assert pa["target"] == {"kind": "CronJob", "namespace": "shop", "name": "report"} and pa["suspend"] is True
    [dry] = cluster.patches
    assert dry["kind"] == "CronJob" and dry["body"] == {"spec": {"suspend": True}} and dry["dry_run"] is True
    assert pa["risk_tier"] == "medium" and pa["estimated_seconds_per_pod_to_ready"] is None
    assert pa["persistence"]["changes"] == [{"object": "CronJob shop/report", "field": "suspend", "from": False, "to": True}]
    assert fx.CANARY not in json.dumps(result)


async def test_suspending_a_suspended_cronjob_is_refused(cluster):
    _obj(cluster, "CronJob", "report")["spec"]["suspend"] = True
    result = await _propose("suspend cronjob report", _change(action="suspend_cronjob"), _CRON)
    assert "already suspended" in result["refusal"]["reason"]


async def test_resume_says_a_missed_run_may_start_at_once(cluster):
    _obj(cluster, "CronJob", "report")["spec"]["suspend"] = True
    pa = (await _propose("resume cronjob report", _change(action="resume_cronjob"), _CRON))["pending_action"]
    assert any("starts one run at once" in r["message"] for r in pa["risks"])
    _obj(cluster, "CronJob", "report")["spec"]["startingDeadlineSeconds"] = 300
    pa = (await _propose("resume cronjob report", _change(action="resume_cronjob"), _CRON))["pending_action"]
    assert not any("starts one run at once" in r["message"] for r in pa["risks"])
    assert any("startingDeadlineSeconds is 300" in n for n in pa["notes"])


async def test_suspend_apply_writes_once_and_verifies(cluster):
    pa = await _approved(cluster, "suspend cronjob report", _change(action="suspend_cronjob"), _CRON)
    result = await _apply(pa)
    assert result["status"] == "succeeded" and _obj(cluster, "CronJob", "report")["spec"]["suspend"] is True
    cluster.patches.clear()
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and not cluster.patches


async def test_a_suspend_undone_while_watched_is_reverted(monkeypatch):
    async def resumed(kind, namespace, name):
        return {"kind": "CronJob", "suspend": False, "status": {}}

    monkeypatch.setattr(read, "get_workload", resumed)
    assert (await rollout.wait_cronjob_suspend("shop", "report", True, 30))["status"] == "reverted"
