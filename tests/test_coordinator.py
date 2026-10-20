import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("a2a")

from chat_gateway.a2a_client import AgentClient  # noqa: E402
from chat_gateway.tasks import artifacts, status_text, task_state  # noqa: E402
from common.settings import get_settings  # noqa: E402
from coordinator import router  # noqa: E402
from coordinator.app import create_app  # noqa: E402
from coordinator.registry import AgentEntry, Registry, RegistryError, load_registry  # noqa: E402
from coordinator.settings import CoordinatorSettings  # noqa: E402
from coordinator.store import RouteStore, TaskLink  # noqa: E402
from tests.ari_cli_fakes import PENDING, PR, FakeAgent, _task  # noqa: E402

KEY = "coordinator-key"
LLM = {"provider": "anthropic", "api_key": "sk-test", "model": "m"}
REPO = {"url": "https://github.com/org/repo", "pat": "ghp_secret"}
CONFIG = {"repo": REPO, "llm": LLM, "options": {"namespace": "apps"}}


class CardAgent(FakeAgent):
    def __init__(self, card):
        super().__init__()
        self.card = card
        self.card_status = 200

    def handler(self, request):
        if request.url.path == "/.well-known/agent-card.json":
            self.hosts.append(request.url.host)
            if self.card_status != 200:
                return httpx.Response(self.card_status, text="down")
            return httpx.Response(200, json=self.card)
        return super().handler(request)


ONCALL_CARD = {"name": "ARI On-Call Agent", "description": "Fixes code and opens pull requests.", "skills": [{"id": "code_analysis_fix", "name": "Code fix", "examples": ["fix the NPE"]}]}
K8S_CARD = {"name": "ARI Kubernetes Agent", "description": "Checks pod health and scales deployments.", "skills": [{"id": "k8s_health", "name": "Pod health"}]}


class Router:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, llm, system, user):
        self.calls.append(json.loads(user))
        answer = self.answers.pop(0) if self.answers else '{"agent": "oncall", "reason": "default"}'
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    monkeypatch.setenv("AGENT_API_KEYS", KEY)
    monkeypatch.setenv("TASK_STORE_ROOT", str(tmp_path / "tasks"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def downstream(oncall, k8s):
    def handle(request):
        return (oncall if request.url.host == "oncall.test" else k8s).handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=5)


def build(oncall, k8s, route_answers=(), root=None):
    registry = Registry(
        [
            AgentEntry("oncall", "http://oncall.test", "oncall-key", ("repo", "llm", "options", "system_prompt")),
            AgentEntry("k8s", "http://k8s.test", "k8s-key", ("llm", "options")),
        ],
        "oncall",
        http=downstream(oncall, k8s),
    )
    settings = CoordinatorSettings(coordinator_poll_seconds=0, coordinator_follow_budget_seconds=30, coordinator_agent_outage_seconds=5)
    complete = Router(route_answers)
    store = RouteStore(str(root) if root else "")
    app = create_app(registry=registry, store=store, settings=settings, complete=complete)
    client = AgentClient("http://coordinator.test", KEY, http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=10))
    return app, client, complete, store


async def settle(client, task_id, want=("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED")):
    for _ in range(200):
        task = await client.get_task(task_id)
        if task_state(task) in want:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"task stayed {task_state(task)}")


@pytest.fixture
def agents():
    return CardAgent(ONCALL_CARD), CardAgent(K8S_CARD)


@pytest.mark.asyncio
async def test_a_kubernetes_question_goes_to_the_k8s_agent_without_the_github_token(env, agents):
    oncall, k8s = agents
    k8s.script("task-1", _task("task-1", "TASK_STATE_WORKING"), _task("task-1", "TASK_STATE_COMPLETED", text="All 12 pods are healthy."))
    app, client, complete, _ = build(oncall, k8s, ['{"agent": "k8s", "reason": "pod health"}'])
    await app.state.refresh()
    task = await client.send("are the pods in apps healthy?", CONFIG)
    done = await settle(client, task["id"])
    assert task_state(done) == "TASK_STATE_COMPLETED"
    assert status_text(done) == "All 12 pods are healthy."
    assert artifacts(done)["route"]["agent"] == "k8s"
    sent = k8s.sends[0]["message"]["parts"][1]["data"]
    assert sent == {"llm": LLM, "options": {"namespace": "apps"}}
    assert "ghp_secret" not in json.dumps(k8s.sends)
    assert oncall.sends == []
    assert [a["agent"] for a in complete.calls[0]["agents"]] == ["oncall", "k8s"]


@pytest.mark.asyncio
async def test_a_proposal_is_relayed_and_the_approval_reaches_the_agents_own_task(env, agents):
    oncall, k8s = agents
    oncall.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready to open a pull request."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened the pull request."),
    )
    app, client, _, _ = build(oncall, k8s, ['{"agent": "oncall", "reason": "code fix"}'])
    await app.state.refresh()
    task = await client.send("fix the NPE", CONFIG)
    paused = await settle(client, task["id"])
    assert task_state(paused) == "TASK_STATE_INPUT_REQUIRED"
    assert artifacts(paused)["pending_action"]["id"] == "act-1"
    assert oncall.sends[0]["message"]["parts"][1]["data"]["repo"]["pat"] == "ghp_secret"
    await client.confirm(paused, "act-1", CONFIG)
    done = await settle(client, task["id"], want=("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"))
    assert task_state(done) == "TASK_STATE_COMPLETED"
    assert artifacts(done)["pull_request"]["url"] == "https://github.com/org/repo/pull/9"
    confirm = oncall.sends[1]["message"]
    assert confirm["task_id"] == "task-1"
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}


@pytest.mark.asyncio
async def test_declining_cancels_the_agents_task_too(env, agents):
    oncall, k8s = agents
    oncall.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    app, client, _, _ = build(oncall, k8s, ['{"agent": "oncall", "reason": "fix"}'])
    await app.state.refresh()
    task = await client.send("fix it", CONFIG)
    await settle(client, task["id"])
    await client.cancel(task["id"])
    assert oncall.cancels == ["task-1"]
    assert task_state(await client.get_task(task["id"])) == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_the_conversation_sticks_to_its_agent_when_routing_fails(env, agents):
    oncall, k8s = agents
    k8s.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="healthy"))
    k8s.script("task-2", _task("task-2", "TASK_STATE_COMPLETED", text="still healthy"))
    app, client, complete, _ = build(oncall, k8s, ['{"agent": "k8s", "reason": "pods"}', RuntimeError("model down")])
    await app.state.refresh()
    first = await client.send("pods ok?", CONFIG, context_id="conv-1")
    await settle(client, first["id"])
    second = await client.send("and now?", CONFIG, context_id="conv-1")
    done = await settle(client, second["id"])
    assert artifacts(done)["route"] == {"agent": "k8s", "reason": "the routing model gave no usable answer", "decided_by": "fallback"}
    assert len(k8s.sends) == 2 and oncall.sends == []
    assert complete.calls[1]["current_agent"] == "k8s"
    assert complete.calls[1]["recent_turns"] == [{"agent": "k8s", "request": "pods ok?", "answer": "healthy"}]
    follow_up = k8s.sends[1]["message"]["parts"][0]["text"]
    assert follow_up.startswith("Earlier in this conversation")
    assert "User: pods ok?" in follow_up and "k8s agent answered: healthy" in follow_up
    assert follow_up.endswith("Current request: and now?")
    assert k8s.sends[0]["message"]["parts"][0]["text"] == "pods ok?"


@pytest.mark.asyncio
async def test_with_one_reachable_agent_no_model_is_asked(env, agents):
    oncall, k8s = agents
    k8s.card_status = 503
    oncall.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="answer"))
    app, client, complete, _ = build(oncall, k8s)
    await app.state.refresh()
    task = await client.send("what is a deadlock?", CONFIG)
    done = await settle(client, task["id"])
    assert artifacts(done)["route"]["decided_by"] == "only"
    assert complete.calls == []
    health = await client._request("GET", "/healthz", "health")
    assert health["agents"]["oncall"] == "available"
    assert health["agents"]["k8s"].startswith("unavailable")


@pytest.mark.asyncio
async def test_no_reachable_agent_fails_the_task_with_the_reason(env, agents):
    oncall, k8s = agents
    oncall.card_status = k8s.card_status = 503
    app, client, _, _ = build(oncall, k8s)
    await app.state.refresh()
    task = await client.send("hello", CONFIG)
    done = await settle(client, task["id"])
    assert task_state(done) == "TASK_STATE_FAILED"
    assert "no agent is reachable" in status_text(done)


@pytest.mark.asyncio
async def test_an_agent_error_is_reported_not_swallowed(env, agents):
    oncall, k8s = agents
    app, client, _, _ = build(oncall, k8s, ['{"agent": "oncall", "reason": "x"}'])
    await app.state.refresh()
    task = await client.send("fix it", CONFIG)
    done = await settle(client, task["id"])
    assert task_state(done) == "TASK_STATE_FAILED"
    assert "oncall" in status_text(done)


@pytest.mark.asyncio
async def test_the_card_offers_every_reachable_agents_skills(env, agents):
    oncall, k8s = agents
    app, client, _, _ = build(oncall, k8s)
    await app.state.refresh()
    card = await client.agent_card()
    assert card["name"] == "ARI"
    assert [s["id"] for s in card["skills"]] == ["oncall.code_analysis_fix", "k8s.k8s_health"]
    oncall.card_status = k8s.card_status = 503
    for agent in app.state.registry.agents:
        agent.card = None
    await app.state.refresh()
    assert [s["id"] for s in (await client.agent_card())["skills"]] == ["route"]


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_refused(env, agents):
    oncall, k8s = agents
    app, _, _, _ = build(oncall, k8s)
    from chat_gateway.a2a_client import AgentError

    anonymous = AgentClient("http://coordinator.test", "wrong", http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))
    with pytest.raises(AgentError) as caught:
        await anonymous.send("hello", CONFIG)
    assert caught.value.status_code in (401, 500)
    assert oncall.sends == [] and k8s.sends == []


@pytest.mark.asyncio
async def test_after_a_restart_a_waiting_proposal_can_still_be_approved(env, agents):
    oncall, k8s = agents
    oncall.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    root = env / "tasks"
    app, client, _, _ = build(oncall, k8s, ['{"agent": "oncall", "reason": "fix"}'], root=root)
    await app.state.refresh()
    task = await client.send("fix it", CONFIG)
    paused = await settle(client, task["id"])
    app2, client2, _, _ = build(oncall, k8s, root=root)
    await app2.state.refresh()
    reloaded = await client2.get_task(task["id"])
    assert task_state(reloaded) == "TASK_STATE_INPUT_REQUIRED"
    await client2.confirm(paused, "act-1", CONFIG)
    done = await settle(client2, task["id"], want=("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"))
    assert task_state(done) == "TASK_STATE_COMPLETED"
    assert oncall.sends[1]["message"]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_a_restart_mid_run_finishes_following_the_agent(env, agents):
    from coordinator.reconciler import reconcile_unfinished_tasks

    oncall, k8s = agents
    oncall.script("task-7", _task("task-7", "TASK_STATE_COMPLETED", text="finished while you were away"))
    root = env / "tasks"
    app, client, _, store = build(oncall, k8s, root=root)
    from a2a.server.context import ServerCallContext
    from a2a.types import Task, TaskState, TaskStatus

    working = Task(id="coord-1", context_id="conv-9", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    await app.state.task_store.save(working, ServerCallContext())
    store.link_task("coord-1", TaskLink("oncall", "task-7", "ctx-1"))
    app2, client2, _, _ = build(oncall, k8s, root=root)
    await reconcile_unfinished_tasks(app2.state.task_store, app2.state.executor)
    done = await client2.get_task("coord-1")
    assert task_state(done) == "TASK_STATE_COMPLETED"
    assert status_text(done) == "finished while you were away"


def test_the_registry_reads_urls_and_keys_from_the_environment(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  - name: oncall\n    url_env: ON_URL\n    api_key_env: ON_KEY\n    forward_config: [repo, llm]\n"
        "  - name: k8s\n    url_env: K8S_URL\n"
        "default_agent: k8s\n",
        encoding="utf-8",
    )
    registry = load_registry(path, {"ON_URL": "http://oncall", "ON_KEY": " secret "})
    oncall, k8s = registry.agents
    assert (oncall.url, oncall.api_key, oncall.forward_config) == ("http://oncall", "secret", ("repo", "llm"))
    assert k8s.url == "" and "K8S_URL" in k8s.error and k8s.forward_config == ("llm", "options")
    assert registry.default_agent == "k8s"
    assert registry.forward(k8s, {**CONFIG, "confirm_pending_action": {"id": "x"}}) == {"llm": LLM, "options": {"namespace": "apps"}, "confirm_pending_action": {"id": "x"}}


def test_the_registry_refuses_configs_that_cannot_work(tmp_path):
    path = tmp_path / "agents.yaml"
    for text, message in (
        ("agents: []\n", "at least one agent"),
        ("agents:\n  - url: http://x\n", "needs a name"),
        ("agents:\n  - name: a\n  - name: a\n", "unique"),
        ("agents:\n  - name: a\ndefault_agent: b\n", "default_agent"),
        ("agents:\n  - name: a\n    forward_config: repo\n", "forward_config"),
    ):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(RegistryError, match=message):
            load_registry(path, {})
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "missing.yaml", {})


@pytest.mark.asyncio
async def test_the_router_never_trusts_an_unknown_answer():
    registry = Registry([AgentEntry("oncall", "http://a", card={}), AgentEntry("k8s", "http://b", card={})], "oncall")
    route = await router.choose(registry, "x", llm=LLM, current="k8s", complete=Router(['{"agent": "jenkins"}']))
    assert (route.agent, route.decided_by) == ("k8s", "fallback")
    route = await router.choose(registry, "x", llm=None, current="")
    assert (route.agent, route.decided_by) == ("oncall", "fallback")
    route = await router.choose(registry, "x", llm=LLM, complete=Router(['Sure! ```json\n{"agent": "k8s", "reason": "pods"}\n```']))
    assert (route.agent, route.reason, route.decided_by) == ("k8s", "pods", "llm")


def test_the_route_store_survives_a_restart_and_keeps_only_recent_turns(tmp_path):
    clock = [1000.0]
    store = RouteStore(str(tmp_path), clock=lambda: clock[0])
    store.link_task("t1", TaskLink("oncall", "d1", "c1"))
    for prompt in ("one", "two", "three"):
        store.remember("conv", "k8s", "kctx", prompt, max_turns=2)
    again = RouteStore(str(tmp_path), clock=lambda: clock[0])
    assert again.task("t1") == TaskLink("oncall", "d1", "c1")
    memory = again.context("conv")
    assert memory["agent"] == "k8s" and memory["downstream"] == {"k8s": "kctx"}
    assert [t["request"] for t in memory["turns"]] == ["two", "three"]
    clock[0] += 8 * 24 * 3600
    assert RouteStore(str(tmp_path), clock=lambda: clock[0]).task("t1") is None


@pytest.mark.asyncio
async def test_follow_up_context_is_bounded_and_can_be_turned_off(env, agents):
    oncall, k8s = agents
    for n in range(1, 6):
        oncall.script(f"task-{n}", _task(f"task-{n}", "TASK_STATE_COMPLETED", text=f"answer {n}"))
    app, client, _, _ = build(oncall, k8s, ['{"agent": "oncall", "reason": "x"}'] * 5)
    app.state.executor.settings.coordinator_context_turns = 2
    await app.state.refresh()
    for n in range(1, 5):
        task = await client.send(f"question {n}", CONFIG, context_id="conv-5")
        await settle(client, task["id"])
    last = oncall.sends[3]["message"]["parts"][0]["text"]
    assert "question 1" not in last and "User: question 2" in last and "answer 3" in last
    app.state.executor.settings.coordinator_context_turns = 0
    task = await client.send("question 5", CONFIG, context_id="conv-5")
    await settle(client, task["id"])
    assert oncall.sends[4]["message"]["parts"][0]["text"] == "question 5"
