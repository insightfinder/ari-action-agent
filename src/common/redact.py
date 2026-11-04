def without_fields(value, fields):
    if not fields:
        return value
    if isinstance(value, dict):
        return {k: without_fields(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list):
        return [without_fields(item, fields) for item in value]
    return value
