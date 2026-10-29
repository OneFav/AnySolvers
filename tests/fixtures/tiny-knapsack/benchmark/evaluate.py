"""Trusted validator and value calculator; never imports candidate code."""
def integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate_input(case):
    if type(case) is not dict or set(case) != {"capacity", "items"}:
        raise ValueError("input requires exactly capacity and items")
    if not integer(case["capacity"], 1, 200):
        raise ValueError("capacity must be integer 1..200")
    items = case["items"]
    if type(items) is not list or not 1 <= len(items) <= 24:
        raise ValueError("items must be list of length 1..24")
    seen = set()
    for item in items:
        if type(item) is not dict or set(item) != {"id", "weight", "value"}:
            raise ValueError("item requires exactly id, weight, value")
        if type(item["id"]) is not str or not item["id"] or item["id"] in seen:
            raise ValueError("item IDs must be nonempty unique strings")
        seen.add(item["id"])
        if not integer(item["weight"], 1, 50) or not integer(item["value"], 1, 100):
            raise ValueError("weight/value must be integers in bounds")


def score(solution, case):
    validate_input(case)
    if type(solution) is not dict or set(solution) != {"selected"}:
        raise ValueError("output requires exactly selected")
    chosen = solution["selected"]
    if type(chosen) is not list or any(type(x) is not str for x in chosen):
        raise ValueError("selected must be a list of strings")
    if len(chosen) != len(set(chosen)):
        raise ValueError("duplicate selection")
    items = {item["id"]: item for item in case["items"]}
    if any(key not in items for key in chosen):
        raise ValueError("unknown item")
    weight = sum(items[key]["weight"] for key in chosen)
    value = sum(items[key]["value"] for key in chosen)
    if weight > case["capacity"]:
        raise ValueError("capacity exceeded")
    return {"score": value, "metrics": {"selected_value": value, "total_weight": weight,
             "selected_count": len(chosen), "unused_capacity": case["capacity"] - weight}}
