import asyncio
import json
import uuid
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console

from chat_gateway.a2a_client import AgentClient, AgentError
from chat_gateway.bridge import Bridge, base_hint, parse_directives
from chat_gateway.config import SECRET_FIELDS, ConfigError, GatewaySettings
from chat_gateway.rendering import Rendered, render, render_outage, render_progress
from chat_gateway.tasks import INPUT_REQUIRED, is_running, pending_action, task_state

from . import views
from .config_file import (
    CONFIG_FILE_ENV,
    config_path,
    history_path,
    load_settings,
    needs_setup,
    read_config_file,
    validation_message,
    write_config_file,
)
from .interactive import COMMANDS, CONNECT_TIMEOUT_SECONDS, ChatSession, run_setup
from .terminal import make_io, safe_streams

__all__ = [
    "CONFIG_FILE_ENV",
    "app",
    "config_path",
    "load_settings",
    "make_bridge",
    "make_client",
    "read_config_file",
    "write_config_file",
]

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="Talk to an ARI agent. Run `ari` on its own for the interactive chat.",
)
tasks_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Inspect or control a task by id.")
config_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Show or change saved settings.")
skills_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Check Agent Skills before installing them.")
app.add_typer(tasks_app, name="tasks")
app.add_typer(config_app, name="config")
app.add_typer(skills_app, name="skills")

REQUESTER = "cli"
REDACTED_KEYS = frozenset({"api_key", "pat", "ssh_key", "token", "password"})


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


def check_connection(settings: GatewaySettings) -> str:
    card = asyncio.run(make_client(settings).agent_card(max_attempts=1, timeout=CONNECT_TIMEOUT_SECONDS))
    return str(card.get("name") or "")


def setup_and_save(io: Any, console: Console, overrides: dict[str, Any] | None = None) -> bool:
    path = config_path()
    try:
        current = read_config_file(path)
    except ConfigError as exc:
        console.print(views.error_line(exc))
        console.print(views.notice_line("Starting from scratch; saving will replace that file."))
        current = {}
    try:
        effective = load_settings(**(overrides or {})).model_dump()
    except ConfigError:
        effective = {}
    values = run_setup(io, console, current, check_connection, effective)
    if values is None:
        return False
    write_config_file(path, values)
    console.print(views.success_line(f"Saved to {path}"))
    return True


def start_interactive(url: str = "", key: str = "", repo: str = "", ref: str = "") -> None:
    console = Console()
    io = make_io(console, history_path(), list(COMMANDS) + ["/exit"])
    overrides = {"ari_agent_url": url, "ari_api_key": key, "default_repo_url": repo, "default_repo_ref": ref}
    settings = settings_or_fail(**overrides)
    if needs_setup(settings, url_given=bool(url)):
        if not setup_and_save(io, console, overrides):
            raise typer.Exit(code=1)
        settings = settings_or_fail(**overrides)

    def setup() -> GatewaySettings | None:
        if not setup_and_save(io, console, overrides):
            return None
        try:
            return load_settings(**overrides)
        except ConfigError as exc:
            console.print(views.error_line(exc))
            return None

    ChatSession(settings, io, console, make_bridge=make_bridge, setup=setup).run()


UrlOption = typer.Option("", "--url", help="agent base URL (ARI_AGENT_URL)")
KeyOption = typer.Option("", "--key", help="agent API key (ARI_API_KEY)")
RepoOption = typer.Option("", "--repo", help="repository URL (DEFAULT_REPO_URL)")
RefOption = typer.Option("", "--ref", help="branch to work from (DEFAULT_REPO_REF)")


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    safe_streams()
    if ctx.invoked_subcommand is None:
        start_interactive()


@app.command()
def chat(url: str = UrlOption, key: str = KeyOption, repo: str = RepoOption, ref: str = RefOption) -> None:
    start_interactive(url, key, repo, ref)


@app.command()
def setup() -> None:
    console = Console()
    io = make_io(console, history_path(), [])
    if not setup_and_save(io, console):
        raise typer.Exit(code=1)


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
            prompt += "\n" + base_hint(directives["base"])
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


@skills_app.command("validate")
def skills_validate(
    paths: list[str] = typer.Argument(..., help="a skill directory, or a directory of skill directories"),
) -> None:
    from pathlib import Path

    from common import skills

    reports = []
    for text in paths:
        path = Path(text)
        if not path.is_dir() or skills.has_skill_file(path):
            reports.append(skills.inspect_skill(path))
            continue
        found = skills.scan(path, "local")
        if not found:
            reports.append(
                skills.Report(
                    path=path,
                    errors=[f"{path} has no {skills.SKILL_FILE}, and none of the folders directly inside it has one"],
                )
            )
        reports.extend(found)
    for report in reports:
        label = report.skill.name if report.skill else str(report.path)
        typer.echo(f"{'ok' if report.ok else 'invalid'}  {label}")
        for error in report.errors:
            typer.echo(f"  error: {error}")
        for warning in report.warnings:
            typer.echo(f"  warning: {warning}")
    invalid = sum(1 for report in reports if not report.ok)
    typer.echo(f"{len(reports) - invalid} valid, {invalid} invalid")
    if invalid:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
