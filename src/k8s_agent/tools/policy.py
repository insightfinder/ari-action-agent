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
# A lowered memory request or limit must be at least this many times the container's current usage (user
# decision 2026-11-26). Never configurable: it guards against an OOM-killed service, not a preference.
DECREASE_USAGE_MARGIN = 1.2

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
# whether the request's own words ask for it. A verb right after a negation ("without a restart", "不重启",
# "don't delete") does not count, and neither do the words health findings and questions use: "restarted 5
# times", "restart count", "restart failed", "重启了很多次", "一直在重启", "why does it restart?".
_RESTART_EN = r"\brestart\b(?!\s*(?:count|policy|loop|times?|fail(?:ed|ing|ures?)?|errors?)\b)"
_RESTART_ZH = (
    r"重启(?!\s*了?\s*(?:\d+|很多|好多|多|好几|几|数|若干|[一二两三四五六七八九十百千]+)\s*[次回遍]"
    r"|\s*次数|\s*策略|\s*失败|\s*不了)"
)
_RESTART_WORDS = (re.compile(rf"(?i)(?P<verb>{_RESTART_EN}|\bbounce\b|\brecycle\b|{_RESTART_ZH})"),)
# "restart pod X" counts: a pod is restarted by deleting it so its controller recreates it (kubectl has no other way).
_POD_VERB = rf"(?P<verb>\b(?:delete|kill|remove)\b|{_RESTART_EN})"
_POD_VERB_ZH = rf"(?P<verb>删除|删掉|杀掉|干掉|{_RESTART_ZH})"
# One pattern per word order, so the check below looks at the words right before the verb itself
# ("pod x 不要删掉": the negation sits between the pod and the verb).
_DELETE_POD_WORDS = (
    re.compile(rf"(?i){_POD_VERB}[^.\n]{{0,60}}\bpods?\b"),
    re.compile(rf"(?i)\bpods?\b[^.\n]{{0,60}}{_POD_VERB}"),
    re.compile(rf"(?i){_POD_VERB_ZH}[^。\n]{{0,30}}pod"),
    re.compile(rf"(?i)pod[^。\n]{{0,30}}{_POD_VERB_ZH}"),
)
# Also what reports an attempt rather than asks for one: "we tried to upgrade it", "it failed to restart".
_NEGATION = re.compile(
    r"(?i)(\b(no|not|without|never|don'?t|do\s+not|avoid|didn'?t|did\s+not|can'?t|cannot|couldn'?t|won'?t|wasn'?t"
    r"|(?:tried|failed|attempted|unable|trying)\s+to)\b[^.\n,;]{0,12}|(不要|不用|不必|无需|不需要|[不无没别勿])\s*)$"
)
# Right before the verb: it describes what happens or asks about it ("keeps", "does it", "一直在", "为什么").
# A question word counts only when no "and", "then" or comma stands between it and the verb: "check why it
# fails and restart checkout" still asks for a restart.
_REPORTED = re.compile(
    r"(?i)((?:\bwhy\b|\bhow\s+(?:often|many|come)\b)(?:(?!\band\b|\bthen\b|,).)*$"
    r"|\b(?:does|did)\s+(?:it|they|the\s+[\w-]+|[\w.-]+)\s*$"
    r"|(?:为什么|为何|怎么)(?:(?!然后|并且|再|和|，|,).)*$"
    r"|(?:一直|不停地?|不断地?|反复|频繁|持续|总是|老是|经常|正在|(?<!现)在|又|会)\s*$)"
)
_SENTENCE_END = re.compile(r"[.?!。？！\n]")


def _asks(patterns, prompt: str, reported: bool = False) -> bool:
    """Whether one of `patterns` matches where the words right before its verb (named group `verb`, else the
    match) are not a negation, nor (with `reported`) a report of or a question about it."""
    text = prompt or ""
    for pattern in patterns:
        for m in pattern.finditer(text):
            at = m.start("verb") if "verb" in pattern.groupindex else m.start()
            before = text[max(0, at - 24) : at]
            before = _SENTENCE_END.split(before)[-1]
            if _NEGATION.search(before) or (reported and _REPORTED.search(before)):
                continue
            return True
    return False


def asks_restart(prompt: str) -> bool:
    """Does the request itself ask for a (rolling) restart?"""
    return _asks(_RESTART_WORDS, prompt, reported=True)


def asks_delete_pod(prompt: str) -> bool:
    """Does the request itself ask for a pod to be deleted?"""
    return _asks(_DELETE_POD_WORDS, prompt, reported=True)


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


def asks_disk_size_change(prompt: str, names: list[str] | None = None) -> bool:
    """Does the request itself ask for a disk's size to change, and nothing about CPU, memory or replicas?
    `names` (the PVC and workload names the request is about) are left out first: a PVC called
    `redis-data-redis-replicas-0` is not a request about replicas."""
    text = prompt or ""
    for name in sorted({n for n in names or [] if n}, key=len, reverse=True):
        text = re.sub(rf"(?i)(?<![\w-]){re.escape(name)}(?![\w-])", " ", text)
    return names_disk(text) and not _NOT_A_DISK_SIZE.search(text) and _asks((_SIZE_CHANGE_WORDS,), text)


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


# A request about a container's startup probe, or about how long it may take to start (II-25740, 2026-11-27).
_STARTUP_WORDS = re.compile(
    r"(?i)(startup\s*-?\s*probe|startupprobe|启动探针|启动探测"
    r"|\b(?:time|long(?:er)?|seconds?|minutes?|mins?|hours?)\b[^.\n]{0,40}\bto\s+(?:start|boot|come\s+up)"
    r"|\bstart(?:ing)?\s*-?\s*up\s+(?:time|timeout|takes|needs|for)\b"
    r"|启动(?:时间|超时|需要|要|慢|太慢)|(?:分钟|秒|小时)[^。\n]{0,10}启动)"
)
_DURATION = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|小时|minutes?|mins?|m|分钟|分|seconds?|secs?|s|秒)(?![a-z])")
_SECONDS = {"h": 3600, "m": 60, "s": 1, "小": 3600, "分": 60, "秒": 1}


def asks_startup_change(prompt: str) -> bool:
    """Does the request itself ask about a startup probe, or about how long a container may take to start?"""
    return bool(_STARTUP_WORDS.search(prompt or ""))


def stated_duration(quote: str | None, prompt: str) -> int | None:
    """Seconds stated by `quote` ("10 minutes", "600s", "1.5 hours", "10分钟"), when the quote is verbatim in the
    request and states exactly one duration with a unit; else None. A bare number is not a duration."""
    if not quote or _normalize(quote) not in _normalize(prompt):
        return None
    found = _DURATION.findall(quote)
    if len(found) != 1:
        return None
    number, unit = found[0]
    seconds = float(number) * _SECONDS[unit[0].lower()]
    return int(seconds) if seconds >= 1 and seconds == int(seconds) else None


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
