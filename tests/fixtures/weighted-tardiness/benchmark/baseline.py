"""Deterministic shortest-processing-time baseline; no optimality claim."""


def solve(case):
    """Return jobs by ascending processing time, then lexicographic job id."""
    return {"order": [job["id"] for job in sorted(case["jobs"], key=lambda j: (j["p"], j["id"]))]}
