def filter_empty(value):
    if isinstance(value, list):
        return [filter_empty(val) for val in value if not (val is None or val == [] or val == {})]
    elif isinstance(value, dict):
        return {key: filter_empty(val) for key, val in value.items() if not (val is None or val == [] or val == {})}
    else:
        return value
