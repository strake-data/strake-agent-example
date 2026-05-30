from fastapi import FastAPI
import uvicorn
import json
import os
import csv
from faker import Faker

app = FastAPI()
faker = Faker()

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOYMENTS_PATH = os.path.join(BASE_DIR, "data/deployments.ndjson")
ONCALL_PATH = os.path.join(BASE_DIR, "data/oncall.csv")

DEPARTMENTS = [
    "Engineering",
    "Product",
    "Design",
    "QA",
    "SRE",
    "Security",
    "Data Science",
]


def get_engineers():
    engineers = []
    if os.path.exists(ONCALL_PATH):
        with open(ONCALL_PATH, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                engineers.append(row["engineer_id"])
    return engineers or [f"eng_{i:03d}" for i in range(20)]


def get_git_deploys():
    engineers = get_engineers()
    if os.path.exists(DEPLOYMENTS_PATH):
        with open(DEPLOYMENTS_PATH, "r") as f:
            deployments = [json.loads(line) for line in f if line.strip()]

            results = []
            for d in deployments[:100]:
                deploy_id = d["deploy_id"]
                # Deterministically pick an engineer for this deployment
                try:
                    # Usually "dep_XXX"
                    deploy_num = int(deploy_id.split("_")[1])
                except (IndexError, ValueError):
                    deploy_num = hash(deploy_id)

                eng_id = engineers[deploy_num % len(engineers)]

                # Seed faker with eng_id for consistent name per engineer
                try:
                    eng_num = int(eng_id.split("_")[1])
                except (IndexError, ValueError):
                    eng_num = hash(eng_id)

                faker.seed_instance(eng_num)

                results.append(
                    {
                        "deploy_id": deploy_id,
                        "commit_sha": d["commit_sha"],
                        "engineer_id": eng_id,
                        "username": faker.name(),
                        "department": faker.random_element(DEPARTMENTS),
                    }
                )
            return results
    return []


@app.get("/git-deploys")
async def git_deploys():
    return get_git_deploys()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8500)
