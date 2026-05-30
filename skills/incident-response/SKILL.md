---
name: incident-response
description: Step-by-step runbook for production incident investigation. Load this first on any incident.
---

# Incident Response Runbook

## Before you start
Call `load_skill('code_analysis')` now. You need both skills active.

## Table names (use these exactly in SQL)
- `alerts` — not sqlite.alerts
- `metrics` — not parquet.metrics  
- `deployments` — not json.deployments
- `oncall`
- `git_deploys`

## The only script you need — copy and adapt this

    from datetime import datetime, timedelta
    from collections import defaultdict

    # Fetch everything in one shot
    alerts  = strake.sql("SELECT alert_id, service, severity, triggered_at, resolved_at FROM alerts WHERE triggered_at >= '2025-03-15 14:00:00' AND triggered_at < '2025-03-15 15:00:00'").to_pylist()
    metrics = strake.sql("SELECT timestamp, service, metric_name, value, pod_id FROM metrics WHERE timestamp >= '2025-03-15 14:00:00' AND timestamp < '2025-03-15 15:00:00'").to_pylist()
    deploys = strake.sql("SELECT deploy_id, service, commit_sha, started_at, changed_files FROM deployments").to_pylist()
    git     = {r['commit_sha']: r for r in strake.sql("SELECT deploy_id, commit_sha, username, department FROM git_deploys").to_pylist()}
    oncall  = {r['primary_service']: r for r in strake.sql("SELECT engineer_id, primary_service, phone FROM oncall").to_pylist()}

    # Summarise metrics
    by_metric = defaultdict(list)
    for r in metrics:
        by_metric[r['metric_name']].append(r['value'])
    for name, vals in sorted(by_metric.items()):
        s = sorted(vals)
        print(f"{name}: n={len(s)} max={max(s):.0f} p50={s[len(s)//2]:.0f}")

    # Correlate alerts → deploys → git → oncall
    for a in alerts:
        svc = a['service']
        t   = datetime.fromisoformat(a['triggered_at'])
        preceding = [d for d in deploys if d['service'] == svc
                     and t - timedelta(minutes=30) <= datetime.fromisoformat(d['started_at']) <= t]
        o = oncall.get(svc, {})
        print(f"\nAlert {a['alert_id']}: {svc} {a['severity']} @ {a['triggered_at']}")
        for d in preceding:
            g = git.get(d['commit_sha'], {})
            print(f"  deploy {d['deploy_id']} by {g.get('username','?')} ({g.get('department','?')})")
            print(f"  files: {d.get('changed_files','?')}")
        print(f"  on-call: {o.get('engineer_id','?')} — {o.get('phone','?')}")

The model doesn't need to understand the pattern — it needs to copy it. A concrete, complete, working template in the skill is more reliable than helper functions that have to be understood and composed.

## Required output format
End every investigation with this exact structure:

    ## Root Cause Summary
    **Service**: <name>
    **Incident window**: <triggered_at> → <resolved_at or 'ongoing'>
    **Root cause**: <one sentence>
    **Triggering deploy**: <deploy_id> — commit <commit_sha>
    **Changed files**: <changed_files>
    **Deployed by**: <username> (<department>)
    **On-call contact**: <engineer_id> — <phone>