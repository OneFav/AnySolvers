# Weighted tardiness on one machine

This scheduling task demonstrates the solver workflow using single-machine
weighted tardiness.

## Problem and objective

There are `n` jobs, all available at time zero, and one machine. Job `j` has
processing time `p_j`, positive weight `w_j`, and due date `d_j`. Process each job
exactly once, without interruption. A solution is a permutation `pi` of the job
IDs. The machine starts at time zero and executes the jobs consecutively in that
order. It does not insert idle time. Completion time and tardiness are

```text
C_{pi(k)} = sum_{i=1..k} p_{pi(i)}
T_j = max(0, C_j - d_j)
objective = sum_j w_j * max(0, C_j - d_j)
```

Minimize the objective. Finishing exactly at a due date incurs zero tardiness.
Weights multiply tardiness; this is not the number of late jobs or the sum of
completion times. All arithmetic in the scorer is exact integer arithmetic.

## Inputs and admissible decisions

One case is a JSON object containing exactly `jobs`. `jobs` is a list of 2 to 12
objects, each containing exactly `id`, `p`, `w`, and `d`:

- `id`: a nonempty string, unique within the case. Whitespace-only IDs are invalid;
  otherwise IDs are compared exactly, without normalization.
- `p`: an integer from 1 to 20, inclusive.
- `w`: an integer from 1 to 10, inclusive.
- `d`: an integer from 0 to 200, inclusive.

Boolean, floating-point, and string values are invalid in integer fields.
Additional or missing object fields are invalid. JSON must not contain repeated
object keys. The scoring API validates the parsed object; a JSON loader must
reject repeated keys before they are lost during parsing.

The decision is a JSON-compatible object containing exactly `order`. `order` is a list
of the input job ID strings, with every job appearing exactly once. Missing,
duplicate, unknown, or non-string IDs are invalid. Candidates should not mutate
the input or rely on state across calls.

```json
{
  "jobs": [
    {"id": "A", "p": 3, "w": 2, "d": 4},
    {"id": "B", "p": 2, "w": 4, "d": 3},
    {"id": "C", "p": 1, "w": 3, "d": 7},
    {"id": "D", "p": 4, "w": 1, "d": 5},
    {"id": "E", "p": 2, "w": 5, "d": 9},
    {"id": "F", "p": 3, "w": 2, "d": 12}
  ]
}
```

For `{"order": ["A", "B", "C", "D", "E", "F"]}`, completion times are
`[3, 5, 6, 10, 12, 15]`, tardiness is `[0, 2, 0, 5, 3, 3]`, and weighted
tardiness is `[0, 8, 0, 5, 15, 6]`. The score is **34**.

## Measurement and information boundary

The primary measure is weighted tardiness. Diagnostic measures are total
tardiness, number of tardy jobs, and makespan; an ordered schedule trace can
explain each contribution. Lower scores are better. A dataset aggregate is the
arithmetic mean only if every case is valid; report validity separately.

All jobs are known at the beginning. There is no hidden future arrival or
interactive state. Compare methods on identical instances with the same resource
limits. Invalid inputs or permutations must be rejected, not assigned a quality
penalty. Dataset preparation owns executable validation, scoring, baselines and
independent checks against the equations and hand calculation above.
