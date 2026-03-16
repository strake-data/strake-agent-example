import sqlite3
import pandas as pd
import numpy as np
import json
import csv
from pathlib import Path
from datetime import datetime, timedelta

# Create data directory
data_dir = Path("data")
data_dir.mkdir(exist_ok=True)


def generate_alerts(n=10000):
    db_path = data_dir / "alerts.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            alert_id TEXT PRIMARY KEY,
            service TEXT,
            severity TEXT,
            triggered_at TIMESTAMP,
            resolved_at TIMESTAMP,
            acknowledged_by TEXT,
            runbook_id TEXT
        )
    """)

    services = [
        "api-gateway",
        "auth-service",
        "payment-service",
        "order-service",
        "inventory-service",
    ]
    severities = ["P0", "P1", "P2", "P3"]
    engineers = [f"eng_{i:03d}" for i in range(20)]

    start_time = datetime(2025, 3, 15, 0, 0, 0)
    data = []
    for i in range(n):
        triggered = start_time + timedelta(seconds=np.random.randint(0, 86400))
        resolved = triggered + timedelta(minutes=np.random.randint(5, 120))
        # Force an alert around 14:30
        if i == 0:
            triggered = datetime(2025, 3, 15, 14, 25, 0)
            resolved = datetime(2025, 3, 15, 15, 0, 0)
            service = "api-gateway"
            severity = "P0"
        else:
            service = np.random.choice(services)
            severity = np.random.choice(severities)

        data.append(
            (
                f"alert_{i:05d}",
                service,
                severity,
                triggered.isoformat(),
                resolved.isoformat(),
                np.random.choice(engineers),
                f"rb_{np.random.randint(100, 999)}",
            )
        )

    cursor.executemany("INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)", data)
    conn.commit()
    conn.close()
    print(f"Generated {n} alerts in {db_path}")


def generate_metrics(n=1000000):
    parquet_path = data_dir / "metrics.parquet"
    services = [
        "api-gateway",
        "auth-service",
        "payment-service",
        "order-service",
        "inventory-service",
    ]
    metric_names = ["api_latency_p99", "throughput", "error_rate"]

    # 1M points over 24 hours for 5 services and 3 metrics
    # ~24 * 60 * 60 / (1,000,000 / 15) = ~1.3 seconds interval

    times = pd.date_range(
        "2025-03-15", periods=n // (len(services) * len(metric_names)), freq="6s"
    )
    df_list = []

    for service in services:
        for metric in metric_names:
            base_value = (
                50.0
                if metric == "api_latency_p99"
                else 100.0
                if metric == "throughput"
                else 0.01
            )
            values = np.random.normal(base_value, base_value * 0.1, len(times))

            # Inject spike at 14:30 for api-gateway latency
            if service == "api-gateway" and metric == "api_latency_p99":
                spike_start = datetime(2025, 3, 15, 14, 25)
                spike_end = datetime(2025, 3, 15, 14, 40)
                mask = (times >= spike_start) & (times <= spike_end)
                values[mask] = np.random.uniform(1500, 2500, mask.sum())

            df_list.append(
                pd.DataFrame(
                    {
                        "timestamp": times,
                        "service": service,
                        "metric_name": metric,
                        "value": values,
                        "pod_id": [
                            f"{service}-{np.random.randint(1, 4)}"
                            for _ in range(len(times))
                        ],
                    }
                )
            )

    df = pd.concat(df_list).sample(frac=1).reset_index(drop=True)
    df.to_parquet(parquet_path)
    print(f"Generated {len(df)} metrics in {parquet_path}")


def generate_deployments(n=200):
    json_path = data_dir / "deployments.ndjson"
    services = [
        "api-gateway",
        "auth-service",
        "payment-service",
        "order-service",
        "inventory-service",
    ]

    start_time = datetime(2025, 3, 15, 0, 0, 0)
    data = []
    for i in range(n):
        started_at = start_time + timedelta(seconds=np.random.randint(0, 86400))

        # Inject deployment before spike
        if i == 0:
            started_at = datetime(2025, 3, 15, 14, 20, 0)
            service = "api-gateway"
            commit_sha = "a1b2c3d4"
            changed_files = ["src/handlers/gateway.go", "config/routes.yaml"]
        else:
            service = np.random.choice(services)
            commit_sha = f"{np.random.randint(0, 0xFFFFFFFF):08x}"
            changed_files = [f"src/file_{j}.py" for j in range(np.random.randint(1, 5))]

        data.append(
            {
                "deploy_id": f"dep_{i:03d}",
                "service": service,
                "commit_sha": commit_sha,
                "started_at": started_at.isoformat(),
                "duration": np.random.randint(60, 300),
                "status": "success",
                "changed_files": changed_files,
            }
        )

    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Generated {n} deployments in {json_path}")


def generate_oncall(n=20):
    csv_path = data_dir / "oncall.csv"
    services = [
        "api-gateway",
        "auth-service",
        "payment-service",
        "order-service",
        "inventory-service",
    ]
    expertise_list = [
        "Kubernetes",
        "Go",
        "Python",
        "React",
        "Postgres",
        "Redis",
        "Networking",
    ]

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["engineer_id", "primary_service", "expertise", "phone", "avg_response_min"]
        )
        for i in range(n):
            writer.writerow(
                [
                    f"eng_{i:03d}",
                    np.random.choice(services),
                    ";".join(np.random.choice(expertise_list, 2, replace=False)),
                    f"+1-555-{np.random.randint(100, 999)}-{i:04d}",
                    np.random.randint(2, 15),
                ]
            )
    print(f"Generated {n} oncall records in {csv_path}")


if __name__ == "__main__":
    generate_alerts()
    generate_metrics()
    generate_deployments()
    generate_oncall()
