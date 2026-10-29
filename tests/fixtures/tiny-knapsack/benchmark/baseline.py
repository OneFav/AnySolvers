"""Input-order feasible greedy baseline, executed only in the sandbox."""
def solve(case):
    remaining = case["capacity"]
    selected = []
    for item in case["items"]:
        if item["weight"] <= remaining:
            selected.append(item["id"])
            remaining -= item["weight"]
    return {"selected": selected}
