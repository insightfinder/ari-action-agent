"""Who last set a field, from the object's `metadata.managedFields`.

The API server records, for every field, the field managers that set it: a write that changes a value moves
the field to the writer's manager (and its subresource, e.g. `scale`, `resize`); a write that leaves the value
as it was does not (both checked on test, 2026-11-27). This agent writes as `client.FIELD_MANAGER`, so a field
that holds the approved value tells whether this agent or someone else put it there.

A path is a list of `fieldsV1` keys as the API server writes them: `["f:spec", "f:replicas"]`, and for a list
item keyed by name `'k:{"name":"app"}'` (see `keyed`).
"""

import json


def keyed(name: str) -> str:
    """The `fieldsV1` key of a list item merged by its name (containers)."""
    return "k:" + json.dumps({"name": name}, separators=(",", ":"))


def _same_key(have: str, want: str) -> bool:
    if have == want:
        return True
    if have.startswith("k:") and want.startswith("k:"):
        try:
            return json.loads(have[2:]) == json.loads(want[2:])
        except ValueError:
            return False
    return False


def _holds(fields: dict, path: list[str]) -> bool:
    node = fields
    for key in path:
        if not isinstance(node, dict):
            return False
        match = next((k for k in node if _same_key(k, key)), None)
        if match is None:
            return False
        node = node[match]
    return True


def owners(managed_fields: list[dict] | None, path: list[str]) -> list[str]:
    """The field managers that own `path` (several when shared), sorted; empty when none is recorded."""
    found = {
        str(entry.get("manager") or "")
        for entry in managed_fields or []
        if _holds(entry.get("fieldsV1") or {}, path)
    }
    return sorted(found - {""})
