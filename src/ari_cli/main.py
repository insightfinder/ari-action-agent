import asyncio
import json
import os
import tomllib
import uuid
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from chat_gateway.a2a_client import AgentClient, AgentError
from chat_gateway.bridge import Bridge, parse_directives
from chat_gateway.config import SECRET_FIELDS, ConfigError, GatewaySettings
from chat_gateway.rendering import Rendered, render, render_outage, render_progress
from chat_gateway.tasks import INPUT_REQUIRED, is_running, pending_action, task_state

app = typer.Typer(no_args_is_help=True, add_completion=False)
tasks_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
app.add_typer(config_app, name="config")

CONFIG_FILE_ENV = "ARI_CONFIG_FILE"
REQUESTER = "cli"
REDACTED_KEYS = frozenset({"api_key", "pat", "ssh_key", "token", "password"})


def config_path() -> Path:
    override = os.environ.get(CONFIG_FILE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "ari" / "config.toml"


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {str(k).lower(): v for k, v in data.items()}


def write_config_file(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    lines = []
    for key, value in sorted(values.items()):
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f"{key} = {json.dumps(str(value), ensure_ascii=False)}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


def load_settings(**overrides: Any) -> GatewaySettings:
    merged = read_config_file(config_path())
    for name in GatewaySettings.model_fields:
        env_value = os.environ.get(name.upper())
        if env_value is not None:
            merged[name] = env_value
    merged.update({k: v for k, v in overrides.items() if v not in (None, "")})
    try:
        return GatewaySettings(**merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid setting: {validation_message(exc)}") from None


def make_client(settings: GatewaySettings) -> AgentClient:
    return AgentClient(settings.ari_agent_url, settings.ari_api_key, timeout=settings.request_timeout_seconds)


def make_bridge(settings: GatewaySettings) -> Bridge:
    return Bridge(make_client(settings), settings)


def fail(message: object, code: int = 1) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=code)


def settings_or_fail(**overrides: Any) -> GatewaySettings:
    try:
        return load_settings(**overrides)
    except ConfigError as exc:
        fail(exc)
        raise


RECOVERABLE = (AgentError, ConfigError, TimeoutError, RuntimeError, ValueError)


def run(coro):
    try:
        return asyncio.run(coro)
    except RECOVERABLE as exc:
        fail(exc)


def attempt(coro):
    try:
        return asyncio.run(coro)
    except RECOVERABLE as exc:
        typer.echo(f"error: {exc}", err=True)
        return None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k in REDACTED_KEYS and v else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def echo_rendered(rendered: Rendered | None) -> None:
    if rendered is None:
        return
    typer.echo(rendered.text)
    for link in rendered.links:
        if link not in rendered.text:
            typer.echo(link)


async def print_progress(rendered: Rendered) -> None:
    typer.echo(f"  … {rendered.text}")


async def print_task_progress(task: dict[str, Any]) -> None:
    if is_running(task):
        await print_progress(render_progress(task))


async def print_outage(error: Exception) -> None:
    await print_progress(render_outage(error))


async def follow(client: AgentClient, settings: GatewaySettings, task_id: str) -> Rendered:
    final = await client.wait(
        task_id,
        poll_seconds=settings.poll_seconds,
        budget_seconds=settings.wait_seconds,
        outage_seconds=settings.agent_outage_seconds,
        on_update=print_task_progress,
        on_outage=print_outage,
    )
    return render(final)


def settings_from_options(url: str, key: str, repo: str, ref: str) -> GatewaySettings:
    return settings_or_fail(ari_agent_url=url, ari_api_key=key, default_repo_url=repo, default_repo_ref=ref)


UrlOption = typer.Option("", "--url", help="agent base URL (ARI_AGENT_URL)")
KeyOption = typer.Option("", "--key", help="agent API key (ARI_API_KEY)")
RepoOption = typer.Option("", "--repo", help="repository URL (DEFAULT_REPO_URL)")
RefOption = typer.Option("", "--ref", help="branch to work from (DEFAULT_REPO_REF)")


@app.command()
def card(url: str = UrlOption, key: str = KeyOption) -> None:
    settings = settings_or_fail(ari_agent_url=url, ari_api_key=key)
    result = run(make_client(settings).agent_card())
    typer.echo(json.dumps(result, indent=2))


@app.command()
def send(
    text: str,
    url: str = UrlOption,
    key: str = KeyOption,
    repo: str = RepoOption,
    ref: str = RefOption,
    base: str = typer.Option("", "--base", help="branch the pull request targets"),
    no_wait: bool = typer.Option(False, "--no-wait", help="print the task id and return"),
) -> None:
    settings = settings_from_options(url, key, repo, ref)
    bridge = make_bridge(settings)
    thread_key = f"cli:{uuid.uuid4()}"
    if base:
        text = f"{text} base={base}"

    async def submit_only() -> str:
        prompt, directives = parse_directives(text)
        if directives.get("base"):
            prompt += f"\nOpen the pull request against the {directives['base']} branch."
        config = bridge.build_config(thread_key, directives)
        task = await bridge.client.send(prompt, config)
        return str(task.get("id", ""))

    if no_wait:
        typer.echo(run(submit_only()))
        return
    rendered = run(bridge.start(thread_key, text, REQUESTER, on_update=print_progress))
    echo_rendered(rendered)
    conversation = bridge.store.get(thread_key)
    if rendered.state == INPUT_REQUIRED and conversation is not None:
        typer.echo(f"\ntask {conversation.task_id}\napprove with: ari approve {conversation.task_id}")
    if rendered.state == "TASK_STATE_FAILED":
        raise typer.Exit(code=1)


@app.command()
def chat(url: str = UrlOption, key: str = KeyOption, repo: str = RepoOption, ref: str = RefOption) -> None:
    settings = settings_from_options(url, key, repo, ref)
    bridge = make_bridge(settings)
    thread_key = f"cli:{uuid.uuid4()}"
    typer.echo("ARI chat. Commands: /approve  /decline  /status  /quit")
    while True:
        try:
            line = typer.prompt("you", prompt_suffix="> ")
        except (EOFError, typer.Abort):
            typer.echo("")
            break
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/approve":
            echo_rendered(attempt(bridge.approve(thread_key, REQUESTER, on_update=print_progress)))
            continue
        if line == "/decline":
            echo_rendered(attempt(bridge.decline(thread_key, REQUESTER)))
            continue
        if line == "/status":
            echo_rendered(attempt(bridge.status(thread_key)))
            continue
        rendered = attempt(bridge.start(thread_key, line, REQUESTER, on_update=print_progress))
        if rendered is None:
            continue
        echo_rendered(rendered)
        conversation = bridge.store.get(thread_key)
        if rendered.state != INPUT_REQUIRED or conversation is None:
            continue
        typer.echo(f"task {conversation.task_id}; approve later with: ari approve {conversation.task_id}")
        try:
            approved = typer.confirm("Approve?", default=False)
        except (EOFError, typer.Abort):
            typer.echo("")
            break
        if approved:
            echo_rendered(attempt(bridge.approve(thread_key, REQUESTER, on_update=print_progress)))


@app.command()
def approve(
    task_id: str,
    pending_id: str = typer.Option("", "--id", help="pending_action id, defaults to the task's own"),
    url: str = UrlOption,
    key: str = KeyOption,
    repo: str = RepoOption,
    ref: str = RefOption,
) -> None:
    settings = settings_from_options(url, key, repo, ref)
    client = make_client(settings)

    async def go() -> Rendered:
        task = await client.get_task(task_id)
        if task_state(task) != INPUT_REQUIRED:
            raise AgentError("approve", 409, f"task is {task_state(task) or 'unknown'}, not waiting for approval")
        action = pending_action(task) or {}
        action_id = pending_id or str(action.get("id") or "")
        if not action_id:
            raise AgentError("approve", 409, "the task carries no pending_action to confirm")
        config = {
            "repo": settings.repo_config(str(action.get("repo_url") or ""), require_pat=True),
            "llm": settings.llm_config(),
            "options": settings.options(),
        }
        resumed = await client.confirm(task, action_id, config, text=f"approved by {REQUESTER}")
        return await follow(client, settings, str(resumed.get("id") or task_id))

    echo_rendered(run(go()))


@tasks_app.command("follow")
def tasks_follow(task_id: str, url: str = UrlOption, key: str = KeyOption) -> None:
    settings = settings_or_fail(ari_agent_url=url, ari_api_key=key)
    rendered = run(follow(make_client(settings), settings, task_id))
    echo_rendered(rendered)
    if rendered.state == "TASK_STATE_FAILED":
        raise typer.Exit(code=1)


@tasks_app.command("get")
def tasks_get(task_id: str, url: str = UrlOption, key: str = KeyOption) -> None:
    settings = settings_or_fail(ari_agent_url=url, ari_api_key=key)
    typer.echo(json.dumps(redact(run(make_client(settings).get_task(task_id))), indent=2))


@tasks_app.command("cancel")
def tasks_cancel(task_id: str, url: str = UrlOption, key: str = KeyOption) -> None:
    settings = settings_or_fail(ari_agent_url=url, ari_api_key=key)
    result = run(make_client(settings).cancel(task_id))
    typer.echo(task_state(result) or "canceled")


@config_app.command("show")
def config_show() -> None:
    settings = settings_or_fail()
    typer.echo(f"config file: {config_path()}")
    for name, value in settings.masked().items():
        typer.echo(f"{name} = {value}")


@config_app.command("set")
def config_set(
    key: str,
    value: str | None = typer.Argument(None, help="omit to be prompted; secrets are prompted without echo"),
) -> None:
    name = key.lower()
    if name not in GatewaySettings.model_fields:
        fail(f"unknown setting {key!r}; known: {', '.join(GatewaySettings.model_fields)}", code=2)
    if value is None:
        value = typer.prompt(name, hide_input=name in SECRET_FIELDS)
    path = config_path()
    values = read_config_file(path)
    values[name] = value
    try:
        GatewaySettings(**values)
    except ValidationError as exc:
        fail(f"invalid value for {name}: {validation_message(exc)}", code=2)
    write_config_file(path, values)
    typer.echo(f"saved {name} to {path}")


if __name__ == "__main__":
    app()
