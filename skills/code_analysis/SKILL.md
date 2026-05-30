---
name: code_analysis
description: Rules for writing efficient multi-source investigation scripts. Load alongside incident-response.
---

# Code-Mode Rules

## The one script rule
A complete investigation should need at most 2 calls to run_python_code:
- Script 1: fetch all five tables, correlate in Python, print findings
- Script 2 (optional): follow-up on a specific finding

If you are on your 3rd sandbox call without a root-cause summary, you are
doing it wrong. Stop, write one script that fetches everything, and finish.

## Never narrow a query to fix truncation
If output is truncated, do NOT re-run with a tighter time window or WHERE
clause. That loop will run forever. Instead:

    # Wrong — truncated, so narrow the window and retry
    metrics = strake.sql("SELECT * FROM metrics WHERE timestamp >= '14:24' AND timestamp <= '14:26'").to_pylist()

    # Right — fetch the full hour, aggregate in Python, print a summary
    metrics = strake.sql(
        "SELECT timestamp, service, metric_name, value FROM metrics "
        "WHERE timestamp >= '2025-03-15 14:00:00' AND timestamp < '2025-03-15 15:00:00'"
    ).to_pylist()
    from collections import defaultdict
    by_metric = defaultdict(list)
    for r in metrics:
        by_metric[r['metric_name']].append(r['value'])
    for name, vals in by_metric.items():
        print(f"{name}: n={len(vals)} max={max(vals):.0f} p50={sorted(vals)[len(vals)//2]:.0f}")

## Timestamp formats — use exactly, never probe with LIKE
| Table        | Column       | Format                  |
|--------------|--------------|-------------------------|
| metrics      | timestamp    | YYYY-MM-DD HH:MM:SS     |
| alerts       | triggered_at | YYYY-MM-DD HH:MM:SS     |
| deployments  | started_at   | YYYY-MM-DDTHH:MM:SS     |

## DISTINCT is broken on alerts — deduplicate in Python
    rows = strake.sql('SELECT service, severity FROM alerts').to_pylist()
    by_service = {r['service']: r for r in rows}

## Join pattern — fetch all, index in Python, never query inside a loop
    deploys = strake.sql("SELECT * FROM deployments").to_pylist()
    git     = strake.sql("SELECT * FROM git_deploys").to_pylist()
    git_by_sha = {r['commit_sha']: r for r in git}
    for d in deploys:
        g = git_by_sha.get(d['commit_sha'], {})
        print(f"{d['deploy_id']} by {g.get('username','?')} ({g.get('department','?')})")

## search_schemas usage
Call search_schemas at most once, only if you are unsure a column name exists.
Do not call it to explore the schema — the skills already list all columns.
Do not call it before task_propose.
Note: search_schemas() is the preferred high-level wrapper, but `strake.search()` is also available in the sandbox if needed.