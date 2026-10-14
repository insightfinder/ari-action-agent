import json
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chat_gateway.config import ConfigError, GatewaySettings

CONFIG_FILE_ENV = "ARI_CONFIG_FILE"
HISTORY_FILE_ENV = "ARI_HISTORY_FILE"


def config_path() -> Path:
    override = os.environ.get(CONFIG_FILE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "ari" / "config.toml"


def history_path() -> Path:
    override = os.environ.get(HISTORY_FILE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "ari" / "history"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML ({exc}); fix it by hand or run: ari setup") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    return {str(k).lower(): v for k, v in data.items()}


def write_config_file(path: Path, values: dict[str, Any]) -> None:
    private_dir(path.parent)
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


def needs_setup(settings: GatewaySettings, url_given: bool = False) -> bool:
    if not settings.llm_api_key:
        return True
    return not config_path().is_file() and "ARI_AGENT_URL" not in os.environ and not url_given
