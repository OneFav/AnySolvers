"""Trusted, standard-library scorer for the weighted-tardiness example task."""


def validate_input(case):
    """Validate one case, raising ValueError on any contract violation.

    JSON integer fields reject booleans and floating-point values. Objects use
    exact keys, so a misspelled field cannot silently change the problem.
    """
    if type(case) is not dict or set(case) != {"jobs"}:
        raise ValueError("case must be an object with exactly the key 'jobs'")
    jobs = case["jobs"]
    if type(jobs) is not list or not 2 <= len(jobs) <= 12:
        raise ValueError("jobs must be a list containing 2 to 12 jobs")

    seen = set()
    for index, job in enumerate(jobs):
        if type(job) is not dict or set(job) != {"id", "p", "w", "d"}:
            raise ValueError(f"jobs[{index}] must have exactly id, p, w, d")
        job_id = job["id"]
        if type(job_id) is not str or not job_id.strip():
            raise ValueError(f"jobs[{index}].id must be a nonempty string")
        if job_id in seen:
            raise ValueError(f"duplicate job id: {job_id!r}")
        seen.add(job_id)
        for field, lower, upper in (("p", 1, 20), ("w", 1, 10), ("d", 0, 200)):
            value = job[field]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(
                    f"jobs[{index}].{field} must be an integer in [{lower}, {upper}]"
                )


def score(solution, case):
    """Return the exact objective, diagnostics, and schedule trace.

    Invalid schedules raise ValueError; they never receive a finite penalty
    that could be mistaken for a valid score by an aggregating evaluator.
    """
    validate_input(case)
    if type(solution) is not dict or set(solution) != {"order"}:
        raise ValueError("solution must be an object with exactly the key 'order'")
    order = solution["order"]
    if type(order) is not list:
        raise ValueError("order must be a list of job ids")
    if len(order) != len(case["jobs"]):
        raise ValueError("order must contain every job exactly once")
    if any(type(job_id) is not str for job_id in order):
        raise ValueError("order entries must be strings")
    if len(set(order)) != len(order):
        raise ValueError("order must not contain duplicate job ids")
    jobs = {job["id"]: job for job in case["jobs"]}
    if set(order) != set(jobs):
        raise ValueError("order must contain exactly the input job ids")

    completion = 0
    weighted_tardiness = 0
    total_tardiness = 0
    tardy_jobs = 0
    trace = []
    for job_id in order:
        job = jobs[job_id]
        start = completion
        completion += job["p"]
        tardiness = max(0, completion - job["d"])
        weighted = job["w"] * tardiness
        weighted_tardiness += weighted
        total_tardiness += tardiness
        tardy_jobs += int(tardiness > 0)
        trace.append(
            {
                "id": job_id,
                "start": start,
                "completion": completion,
                "due": job["d"],
                "tardiness": tardiness,
                "weight": job["w"],
                "weighted_tardiness": weighted,
            }
        )
    return {
        "score": weighted_tardiness,
        "metrics": {
            "weighted_tardiness": weighted_tardiness,
            "total_tardiness": total_tardiness,
            "tardy_jobs": tardy_jobs,
            "makespan": completion,
        },
        "trace": trace,
    }
