---
name: incident-response
description: Standard operating procedure for production incident investigation.
---
# Incident Response Protocol

Follow these steps when investigating a production incident:

1. **Verify the Alert**: Use `run_python_code` to check `sqlite.alerts` and confirm the severity and affected service.
2. **Observe Symptoms**: Check `parquet.metrics` for latency spikes, error rates, or resource exhaustion around the alert time.
3. **Check Recent Changes**: Query `json.deployments` to see if any service was updated just before the incident started.
4. **Identify Ownership**: Use `oncall` data to find the primary engineer for the affected service.
5. **Correlate and Confirm**: Join metrics with git metadata if possible to pinpoint specific commits that might be responsible.

## Guidelines
- Always start with a `todo` to plan these steps.
- Summarize findings before escalating or concluding.