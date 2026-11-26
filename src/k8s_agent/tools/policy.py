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
DEFAULT_HIGH_REPLICA_THRESHOLD = 30  # the user's choice, 2026-11-24 (was 50)

_AUTOSCALER_WORDS = re.compile(
    r"(?i)(keda|scaled\s*-?\s*object|\bhpa\b|horizontal\s*pod\s*autoscaler|autoscal\w*|自动扩缩|弹性伸缩)"
)
# Wording that asks for an in-place pod resize rather than a Deployment change. A pod name alone is not enough:
# on-call prompts quote pod names from health findings while asking for the Deployment's memory to change.
_IN_PLACE_WORDS = re.compile(
    r"(?i)(in[\s-]?place|\bresize\b[^.\n]{0,40}\bpod\b|\bpod\b[^.\n]{0,40}\bresize\b|without\s+(a\s+)?(restart|rollout|recreat\w*|redeploy\w*)"
    r"|no\s+(rollout|restart|recreat\w*)|原地|不重建|不重启|不滚动)"
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


def asks_in_place(prompt: str) -> bool:
    """Does the request itself ask for an in-place pod resize (not just name a pod)?"""
    return bool(_IN_PLACE_WORDS.search(prompt or ""))


# Requests for a change that takes no value (restart, delete). The model picks the action; these decide
# whether the request's own words ask for it. A match right after a negation ("without a restart", "不重启",
# "don't delete") does not count, and neither do the words health findings use ("restarted 5 times", "restart
# count", "重启了 5 次").
_RESTART_WORDS = re.compile(
    r"(?i)(\brestart\b(?!\s*(?:count|policy|loop|times?)\b)|\bbounce\b|\brecycle\b|重启(?!\s*了?\s*\d+\s*次|次数|策略))"
)
# "restart pod X" counts: a pod is restarted by deleting it so its controller recreates it (kubectl has no other way).
_POD_VERB = r"(?:delete|kill|remove|restart(?!\s*(?:count|policy|loop|times?)\b))"
_POD_VERB_ZH = r"(?:删除|删掉|杀掉|干掉|重启(?!\s*了?\s*\d+\s*次|次数|策略))"
_DELETE_POD_WORDS = re.compile(
    rf"(?i)(\b{_POD_VERB}\b[^.\n]{{0,60}}\bpods?\b|\bpods?\b[^.\n]{{0,60}}\b{_POD_VERB}\b"
    rf"|{_POD_VERB_ZH}[^。\n]{{0,30}}pod|pod[^。\n]{{0,30}}{_POD_VERB_ZH})"
)
# Also what reports an attempt rather than asks for one: "we tried to upgrade it", "it failed to restart".
_NEGATION = re.compile(
    r"(?i)(\b(no|not|without|never|don'?t|do\s+not|avoid|didn'?t|did\s+not|can'?t|cannot|couldn'?t|won'?t|wasn'?t"
    r"|(?:tried|failed|attempted|unable|trying)\s+to)\b[^.\n,;]{0,12}|(不要|不用|不必|无需|不需要|[不无没别勿])\s*)$"
)


def _asks(pattern: re.Pattern, prompt: str) -> bool:
    text = prompt or ""
    return any(not _NEGATION.search(text[max(0, m.start() - 24) : m.start()]) for m in pattern.finditer(text))


def asks_restart(prompt: str) -> bool:
    """Does the request itself ask for a (rolling) restart?"""
    return _asks(_RESTART_WORDS, prompt)


def asks_delete_pod(prompt: str) -> bool:
    """Does the request itself ask for a pod to be deleted?"""
    return _asks(_DELETE_POD_WORDS, prompt)


# A disk (PVC) size change: the request names a disk and asks for it to be larger (or smaller, which is refused).
_DISK_WORDS = re.compile(r"(?i)(\b(?:pvcs?|persistentvolumeclaims?|persistent\s+volume\s+claims?|disks?|volumes?|storage)\b|磁盘|硬盘|存储|卷)")
_SIZE_CHANGE_WORDS = re.compile(
    r"(?i)(\b(?:expand|grow|resize|increase|enlarge|extend|raise|bigger|larger|add|shrink|reduce|decrease|smaller)\b"
    r"|扩容|扩大|扩到|扩展|增大|加大|调大|增加|加|缩小|缩容|减小|调小)"
)


# Words of a request about something else that also says disk/volume/storage: container resources, replicas,
# a node's disk pressure, ephemeral storage.
_NOT_A_DISK_SIZE = re.compile(
    r"(?i)(\b(?:cpu|memory|mem|replicas?|ephemeral[-\s]?storage|disk\s*pressure|diskpressure|limits?)\b|内存|副本)"
)


def names_disk(prompt: str) -> bool:
    """Does the request talk about a disk (PVC, volume, storage)?"""
    text = _NOT_A_DISK_SIZE.sub(" ", prompt or "")
    return bool(_DISK_WORDS.search(text))


def asks_disk_size_change(prompt: str) -> bool:
    """Does the request itself ask for a disk's size to change, and nothing about CPU, memory or replicas?"""
    return names_disk(prompt) and not _NOT_A_DISK_SIZE.search(prompt or "") and _asks(_SIZE_CHANGE_WORDS, prompt)


# A storage size as people write it: 20Gi, 20 GiB, 500G, 1.5TB. Units are required, so a PVC name's ordinal
# ("data-loki-0") or a count is never read as a size.
_STORAGE_SIZE = re.compile(r"(?i)(?<![\w.])(\d+(?:\.\d+)?)\s*(kib|mib|gib|tib|pib|ki|mi|gi|ti|pi|kb|mb|gb|tb|pb|k|m|g|t|p)(?![\w])")
_STORAGE_UNIT = {
    "ki": "Ki", "kib": "Ki", "mi": "Mi", "mib": "Mi", "gi": "Gi", "gib": "Gi", "ti": "Ti", "tib": "Ti", "pi": "Pi", "pib": "Pi",
    "k": "k", "kb": "k", "m": "M", "mb": "M", "g": "G", "gb": "G", "t": "T", "tb": "T", "p": "P", "pb": "P",
}
# Words right before a size that make it an amount to add, not the size to reach.
_RELATIVE = re.compile(r"(?i)(\bby|\badd(?:ing)?|\bmore|\+|增加了?|加|多|再)\s*$")
# Words right before a size that make it the size to reach.
_TARGET = re.compile(r"(?i)(\bto|\binto|到|至|为|成)\s*$")
# Words right before a size that make it the size now ("from 10Gi to 20Gi").
_FROM = re.compile(r"(?i)(\bfrom|从|由)\s*$")


def requested_storage(prompt: str) -> tuple[dict | None, str | None]:
    """`({"value": "20Gi", "quote": "20 GiB", "relative": False}, None)`: the one size the request names, as a
    Kubernetes quantity (`G` is 10^9, `Gi` 2^30, as Kubernetes reads them), or `(None, why not)`. `relative` is True
    for an amount to add ("add 10Gi", "加 10Gi", "by 10Gi"; user decision 2026-11-26): the caller adds it to the
    current capacity and shows the sum. Several sizes need exactly one that is the target or the amount, and the
    rest marked as the size now ("from 10Gi to 20Gi"); a size to reach and an amount to add together are refused."""
    text = prompt or ""
    sizes = []
    for m in _STORAGE_SIZE.finditer(text):
        before = text[max(0, m.start() - 12) : m.start()]
        sizes.append(
            {
                "value": f"{m.group(1)}{_STORAGE_UNIT[m.group(2).lower()]}",
                "quote": m.group(0),
                "relative": bool(_RELATIVE.search(before)),
                "target": bool(_TARGET.search(before)),
                "from": bool(_FROM.search(before)),
            }
        )
    if not sizes:
        return None, "The request names no size with a unit (for example 20Gi)."
    relative = [s for s in sizes if s["relative"]]
    targets = [s for s in sizes if s["target"] and not s["relative"]]
    if relative and targets:
        return None, (
            f"The request names both a size to reach ({targets[0]['quote']}) and an amount to add ({relative[0]['quote']}); "
            "name only one."
        )
    chosen = relative or targets or (sizes if len(sizes) == 1 else [])
    if len(chosen) == 1 and all(s["from"] for s in sizes if s is not chosen[0]):
        return {"value": chosen[0]["value"], "quote": chosen[0]["quote"], "relative": chosen[0]["relative"]}, None
    return None, "The request names several sizes (" + ", ".join(s["quote"] for s in sizes) + "); name only the size the disk should have."


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
