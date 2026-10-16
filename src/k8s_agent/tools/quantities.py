"""Kubernetes resource quantities as integers this agent can compare.

CPU is handled in millicores and memory in bytes, both rounded *up* the
way the API server does, so "is this request above the cap" never
disagrees with what the cluster would store. Formatting goes the other
way: a clamped value is rounded *down* (to 1m / 1Mi) so it can never end
up a hair above the cap it was clamped to.
"""

import math
import re
from decimal import Decimal, InvalidOperation

_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}
_QUANTITY = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]?)\s*$")

MI = 2**20
GI = 2**30


def parse_quantity(value) -> Decimal:
    """A quantity string ("500m", "2Gi", "1.5", "1e3") or number, in base units."""
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    match = _QUANTITY.match(str(value))
    if not match:
        raise ValueError(f"not a Kubernetes quantity: {value!r}")
    number, suffix = match.groups()
    try:
        base = Decimal(number)
    except InvalidOperation:
        raise ValueError(f"not a Kubernetes quantity: {value!r}") from None
    if suffix in _BINARY:
        return base * _BINARY[suffix]
    return base * _DECIMAL[suffix]


def cpu_millis(value) -> int:
    return math.ceil(parse_quantity(value) * 1000)


def memory_bytes(value) -> int:
    return math.ceil(parse_quantity(value))


def format_cpu(millis: int) -> str:
    if millis % 1000 == 0:
        return str(millis // 1000)
    return f"{millis}m"


def format_memory(num_bytes: int) -> str:
    if num_bytes % GI == 0:
        return f"{num_bytes // GI}Gi"
    if num_bytes % MI == 0:
        return f"{num_bytes // MI}Mi"
    if num_bytes % 2**10 == 0:
        return f"{num_bytes // 2**10}Ki"
    return str(num_bytes)


# Smallest memory value a person plausibly means. Below it the value is almost always a unit slip
# ("512" or "512m" for 512Mi), which the API server would accept as bytes and roll out.
MIN_MEMORY_BYTES = 4 * MI


def memory_request_problem(value) -> str | None:
    """Why a requested memory value is refused, or None. `m`/`u`/`n` are legal suffixes for memory
    (milli-bytes) but nobody means them, and many tools write "512m" for megabytes."""
    match = _QUANTITY.match(str(value))
    if match and match.group(2) in ("m", "u", "n"):
        return (
            f"memory value {value!r} uses the suffix {match.group(2)!r}, which Kubernetes reads as fractions of "
            "a byte; write it with Mi or Gi (e.g. 512Mi)."
        )
    try:
        amount = memory_bytes(value)
    except (ValueError, TypeError):
        return None  # reported as an invalid quantity by the caller
    if 0 < amount < MIN_MEMORY_BYTES:
        return f"memory value {value!r} is {amount} bytes; write it with Mi or Gi (e.g. 512Mi)."
    return None


def floor_memory_to_mi(num_bytes: int) -> int:
    return num_bytes - num_bytes % MI


def to_units(resource: str, value) -> int:
    """`cpu` -> millicores, `memory` -> bytes."""
    if resource == "cpu":
        return cpu_millis(value)
    if resource == "memory":
        return memory_bytes(value)
    raise ValueError(f"unsupported resource {resource!r}")


def format_units(resource: str, amount: int) -> str:
    return format_cpu(amount) if resource == "cpu" else format_memory(amount)
