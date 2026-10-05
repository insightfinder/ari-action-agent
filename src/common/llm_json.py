"""Pulling a JSON object back out of an LLM's prose.

Asking a model for "ONLY JSON" is a request, not a constraint: a fenced
block, a sentence of preamble, or a trailing explanation all still arrive
regularly, and `json.loads` rejects every one of them. This is the shared
tolerant reader for the three shapes seen in practice -- text after a
sentinel, a ```json fence, and bare braces somewhere in prose.

Tolerant about packaging, strict about content: it never returns a
half-parsed object, and it never guesses. A caller that cannot use the
result gets an exception, not a plausible-looking dict.
"""

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract(text: str, sentinel: str | None = None) -> dict:
    """The object following `sentinel`, or -- failing that -- a fenced or
    trailing JSON object anywhere in `text`.

    `sentinel` is optional: the planner asks for a bare object with no
    marker at all, while the capabilities that prompt for a proposal use
    one. Both end up here rather than in two slightly different readers.

    Raises rather than returning None: every caller treats a missing
    object as fatal or as a reason to fall back, and a shared `raise`
    keeps the message consistent instead of several slightly different
    ones.
    """
    candidates: list[str] = []

    if sentinel and sentinel in text:
        candidates.append(text.rsplit(sentinel, 1)[1].strip())
    candidates.extend(_FENCE.findall(text))
    # Last resort: the outermost brace pair. Cheap, and covers a model
    # that answered with bare JSON wrapped in a sentence of preamble.
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    for candidate in candidates:
        # A sentinel-suffixed block may still be followed by prose; peel
        # back to the outermost braces before parsing.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    where = f" (expected after {sentinel!r})" if sentinel else ""
    raise RuntimeError(
        f"could not extract a JSON object{where} from the model's response: {text[:2000]}"
    )
