import json

import httpx

RUNNING = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")


def _task(task_id, state, artifacts=None, text="", **extra):
    task = {"id": task_id, "contextId": "ctx-1", "status": {"state": state}, **extra}
    if text:
        task["status"]["message"] = {"parts": [{"text": text}]}
    if artifacts:
        task["artifacts"] = artifacts
    return task


def proposal(**overrides):
    data = {
        "id": "act-1",
        "action_name": "Open pull request",
        "title": "Fix it",
        "repo_url": "https://github.com/org/other",
        "branch": "ari/fix-it",
        "base_ref": "dev",
        "compare_url": "https://github.com/org/other/compare/dev...b",
    }
    data.update(overrides)
    return [{"name": "pending_action", "parts": [{"data": data}]}]


PENDING = proposal()
PR = [{"name": "pull_request", "parts": [{"data": {"url": "https://github.com/org/repo/pull/9"}}]}]


class FakeAgent:
    def __init__(self):
        self.sends = []
        self.polls = {}
        self.cancels = []
        self.hosts = []
        self.counter = 0
        self.outage_after_confirm = 0
        self.failing_gets = {}
        self.after_follow_up = {}

    def script(self, task_id, *tasks):
        self.polls[task_id] = list(tasks)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        path = request.url.path
        if path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={"name": "ARI On-Call Agent", "skills": [{"id": "code_analysis_fix"}]})
        if path == "/message:send":
            body = json.loads(request.read())
            self.sends.append(body)
            message = body["message"]
            task_id = message.get("task_id")
            if not task_id:
                self.counter += 1
                return httpx.Response(200, json={"task": _task(f"task-{self.counter}", "TASK_STATE_SUBMITTED")})
            queue = self.polls.get(task_id) or []
            data = message["parts"][1].get("data", {})
            if queue and queue[0]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED":
                if "confirm_pending_action" in data:
                    queue.pop(0)
                    self.failing_gets[task_id] = self.outage_after_confirm
                elif task_id in self.after_follow_up:
                    self.polls[task_id] = list(self.after_follow_up.pop(task_id))
            elif queue and queue[0]["status"]["state"] not in RUNNING:
                return httpx.Response(400, text=f"Task {task_id} is in a terminal state")
            return httpx.Response(200, json={"task": _task(task_id, "TASK_STATE_SUBMITTED")})
        if path.startswith("/tasks/") and path.endswith(":cancel"):
            task_id = path[len("/tasks/") : -len(":cancel")]
            self.cancels.append(task_id)
            self.polls[task_id] = [_task(task_id, "TASK_STATE_CANCELED")]
            return httpx.Response(200, json=_task(task_id, "TASK_STATE_CANCELED"))
        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/") :]
            if self.failing_gets.get(task_id, 0) > 0:
                self.failing_gets[task_id] -= 1
                return httpx.Response(503, text="no available server")
            queue = self.polls.get(task_id)
            if not queue:
                return httpx.Response(404, text=f"Task {task_id} not found")
            head = queue[0]
            if head["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" or len(queue) == 1:
                return httpx.Response(200, json=head)
            return httpx.Response(200, json=queue.pop(0))
        return httpx.Response(404, text=f"no route {path}")
