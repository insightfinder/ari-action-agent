"""Rules that must hold even when the model misbehaves.

Everything here is decided in code, never by the LLM: whether a request
explicitly asked for an autoscaler or resource change, the replica floor,
and the risk tier shown to the approver. Logs, events and annotations
reach the model's context and can carry hostile text; none of them can
argue with a regex or an integer comparison.
"""

import re

from . import quantities

REPLICA_FLOOR = 1  # never configurable: nothing this agent does scales a workload to zero
DEFAULT_HIGH_REPLICA_THRESHOLD = 50

_AUTOSCALER_WORDS = re.compile(
    r"(?i)(keda|scaled\s*-?\s*object|\bhpa\b|horizontal\s*pod\s*autoscaler|autoscal\w*|自动扩缩|弹性伸缩)"
)
_DIGIT = re.compile(r"\d")
_SPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _SPACE.sub(" ", text or "").strip().casefold()


_AMOUNT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")


def _states(quote: str, value, resource: str | None) -> bool:
    """Whether some amount written in `quote` is `value`: a replica count as the same integer, a
    CPU/memory value as the same quantity however it is written ("2Gi", "2 GiB", "0.5 cores" for 500m)."""
    for number, unit in _AMOUNT.findall(quote):
        if resource is None:
            if float(number) == float(value):
                return True
            continue
        unit = unit.lower()
        spellings = [number + unit[:-1] if unit.endswith("ib") else number + unit, number]
        if unit.endswith("b") and not unit.endswith("ib"):
            spellings.insert(1, number + unit[:-1])
        for spelling in spellings:
            if spelling != number and not unit:
                continue
            if spelling == number and unit and resource == "memory":
                continue  # "2 apples" is not 2 bytes
            try:
                if quantities.to_units(resource, _k8s_case(spelling)) == quantities.to_units(resource, value):
                    return True
            except (ValueError, TypeError):
                continue
    return False


def _k8s_case(spelling: str) -> str:
    """`2gi` -> `2Gi`, `500m` stays `500m` (millicores), `2g` -> `2G`: Kubernetes suffixes are case-sensitive."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]*)", spelling)
    if not match:
        return spelling
    number, unit = match.groups()
    if unit in ("", "m"):
        return number + unit
    if unit.endswith("i"):
        return number + unit[0].upper() + "i"
    return number + unit.upper()


def quote_in_prompt(quote: str | None, prompt: str, value=None, resource: str | None = None) -> bool:
    """The model must back every number it extracted with a verbatim quote
    from the request: the quote must appear in it, and must state the very
    value extracted (`resource` None for a replica count, else `cpu` or
    `memory`), so "3 replicas" cannot back 2024 and "2Gi" cannot back 20Gi."""
    if not quote or not _DIGIT.search(quote):
        return False
    if _normalize(quote) not in _normalize(prompt):
        return False
    return value is None or _states(quote, value, resource)


def mentions_autoscaler(prompt: str, own_names: list[str] | None = None) -> bool:
    """Does the request itself name an autoscaler? The target's own names are
    removed first: a Deployment called `web-hpa` or `queue-keda-worker` must
    not turn "let web-hpa go up to 10" into an explicit autoscaler request
    (found on the test cluster, 2026-10-13)."""
    text = prompt or ""
    for name in sorted({n for n in own_names or [] if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return bool(_AUTOSCALER_WORDS.search(text))


def high_replica_threshold(options: dict) -> int:
    raw = (options or {}).get("high_replica_threshold")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HIGH_REPLICA_THRESHOLD
    return value if value > 0 else DEFAULT_HIGH_REPLICA_THRESHOLD


def headroom_ratio(options: dict) -> float:
    raw = (options or {}).get("node_headroom_ratio")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.2
    return value if 0 <= value < 0.9 else 0.2


_TIER_ORDER = ["low", "medium", "high", "critical"]


def raise_tier(current: str, at_least: str) -> str:
    return max(current, at_least, key=_TIER_ORDER.index)


def base_tier(action: str, details: dict) -> str:
    """The table: same action, same facts, same tier, every time."""
    if action == "scale_replicas":
        return "medium" if details.get("scale_down") else "low"
    if action == "set_autoscaler_bounds":
        return "medium"
    if action == "set_resources":
        return "medium"  # every resource change restarts pods
    return "high"
