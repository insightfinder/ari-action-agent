import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
import structlog
import yaml

from chat_gateway.a2a_client import AgentClient
from common.config_paths import configs_dir

logger = structlog.get_logger()

ALWAYS_FORWARDED = ("confirm_pending_action",)
DEFAULT_FORWARD = ("llm", "options")
MAX_EXAMPLES = 3


class RegistryError(ValueError):
    pass


@dataclass
class AgentEntry:
    name: str
    url: str
    api_key: str = ""
    forward_config: tuple[str, ...] = DEFAULT_FORWARD
    card: dict[str, Any] | None = None
    error: str = "not checked yet"

    @property
    def available(self) -> bool:
        return self.card is not None


class Registry:
    def __init__(
        self,
        agents: list[AgentEntry],
        default_agent: str = "",
        *,
        timeout: float = 60.0,
        card_timeout: float = 10.0,
        http: httpx.AsyncClient | None = None,
    ):
        names = [agent.name for agent in agents]
        if not agents:
            raise RegistryError("no agents are configured")
        if len(set(names)) != len(names):
            raise RegistryError(f"agent names must be unique: {names}")
        if default_agent and default_agent not in names:
            raise RegistryError(f"default_agent {default_agent!r} is not one of {names}")
        self.agents = agents
        self.default_agent = default_agent or names[0]
        self.timeout = timeout
        self.card_timeout = card_timeout
        self._http = http

    def get(self, name: str) -> AgentEntry | None:
        return next((agent for agent in self.agents if agent.name == name), None)

    def available(self) -> list[AgentEntry]:
        return [agent for agent in self.agents if agent.available]

    def client(self, agent: AgentEntry) -> AgentClient:
        return AgentClient(agent.url, agent.api_key, timeout=self.timeout, http=self._http)

    def forward(self, agent: AgentEntry, config: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(agent.forward_config) | set(ALWAYS_FORWARDED)
        return {key: value for key, value in config.items() if key in allowed}

    async def refresh(self) -> None:
        await asyncio.gather(*(self._refresh_one(agent) for agent in self.agents))

    async def _refresh_one(self, agent: AgentEntry) -> None:
        if not agent.url:
            return
        try:
            card = await self.client(agent).agent_card(max_attempts=1, timeout=self.card_timeout)
        except Exception as exc:
            agent.error = str(exc)[:300] or type(exc).__name__
            logger.warning("coordinator_agent_unreachable", agent=agent.name, error=agent.error, kept_card=agent.card is not None)
            return
        agent.card = card
        agent.error = ""

    def describe(self, agent: AgentEntry) -> dict[str, Any]:
        card = agent.card or {}
        skills = []
        for skill in card.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            skills.append(
                {
                    "name": skill.get("name") or skill.get("id") or "",
                    "description": skill.get("description") or "",
                    "examples": [str(e) for e in (skill.get("examples") or [])][:MAX_EXAMPLES],
                }
            )
        return {"agent": agent.name, "title": card.get("name") or agent.name, "description": card.get("description") or "", "skills": skills}

    def status(self) -> dict[str, str]:
        return {agent.name: "available" if agent.available else f"unavailable: {agent.error}" for agent in self.agents}


def default_agents_file() -> Path:
    return configs_dir() / "coordinator" / "agents.yaml"


def load_registry(
    path: str | os.PathLike | None = None,
    environ: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Registry:
    env = os.environ if environ is None else environ
    source = Path(path) if path else default_agents_file()
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise RegistryError(f"agents file not found: {source}") from None
    except yaml.YAMLError as exc:
        raise RegistryError(f"agents file {source} is not valid YAML: {exc}") from None
    items = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RegistryError(f"{source} must list at least one agent under 'agents'")
    agents = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise RegistryError(f"every agent in {source} needs a name")
        name = str(item["name"]).strip()
        url_env = str(item.get("url_env") or "")
        url = (env.get(url_env, "") if url_env else "") or str(item.get("url") or "")
        api_key_env = str(item.get("api_key_env") or "")
        forward = item.get("forward_config")
        if forward is not None and (not isinstance(forward, list) or not all(isinstance(k, str) for k in forward)):
            raise RegistryError(f"forward_config of agent {name!r} must be a list of config keys")
        agent = AgentEntry(
            name=name,
            url=url.strip(),
            api_key=(env.get(api_key_env, "") if api_key_env else "").strip(),
            forward_config=tuple(forward) if forward is not None else DEFAULT_FORWARD,
        )
        if not agent.url:
            agent.error = f"no URL configured (set {url_env})" if url_env else "no URL configured"
        agents.append(agent)
    default = str(data.get("default_agent") or "")
    return Registry(agents, default, **kwargs)
