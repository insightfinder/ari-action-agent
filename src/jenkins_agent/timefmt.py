import re
from datetime import datetime, timezone

TIME_RULE = (
    "Jenkins reports times (`timestamp`) and durations (`duration`, `estimatedDuration`) "
    "as milliseconds. Never convert them yourself: write a time as [ts:<milliseconds>] and "
    "a duration as [dur:<milliseconds>], copying the number exactly as Jenkins gave it, "
    "for example [ts:1791454828356] or [dur:484357]. They are turned into dates and "
    "durations after you answer."
)

_EPOCH_MS_MIN = 978307200000
_EPOCH_MS_MAX = 4102444800000

_TS_RE = re.compile(r"\[ts:\s*(\d{1,15})\s*\]")
_DUR_RE = re.compile(r"\[dur:\s*(\d{1,15})\s*\]")
_BARE_RE = re.compile(r"(?<![\w.:\[])(\d{13})(?![\w.])(?!\s*\()")


def format_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    seconds = round(ms / 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _in_range(ms: int) -> bool:
    return _EPOCH_MS_MIN <= ms <= _EPOCH_MS_MAX


def humanize(text: str) -> str:
    if not text:
        return text

    def ts(match: re.Match) -> str:
        ms = int(match.group(1))
        return format_time(ms) if _in_range(ms) else match.group(0)

    def bare(match: re.Match) -> str:
        ms = int(match.group(1))
        return f"{match.group(1)} ({format_time(ms)})" if _in_range(ms) else match.group(0)

    text = _TS_RE.sub(ts, text)
    text = _DUR_RE.sub(lambda m: format_duration(int(m.group(1))), text)
    return _BARE_RE.sub(bare, text)


def with_time_rule(instruction: str) -> str:
    return f"{instruction}\n\n{TIME_RULE}"
