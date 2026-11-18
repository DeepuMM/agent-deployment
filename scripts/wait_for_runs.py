"""Waits for other pipelines' runs of the SAME commit, so separate pipelines keep their order:
tool-deploy ─► use-case-onboarding ─► agent-deploy.

  wait_for_runs.py <workflow file> ...        e.g. tool-deploy.yml use-case-onboarding.yml

For each workflow: finds its run for this commit and event (GITHUB_SHA, GITHUB_EVENT_NAME), waits until it
completes, and fails unless it succeeded. The caller passes only the workflows this commit actually triggers
(scripts/pipeline_plan.py), so nothing waits for a run that will never exist; a run that hasn't appeared after a few
minutes is reported as missing. Needs GITHUB_TOKEN with actions: read.
"""
import os
import sys
import time

import requests

API = "https://api.github.com"


def runs(workflow):
    r = requests.get(f"{API}/repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/{workflow}/runs",
                     params={"head_sha": os.environ["GITHUB_SHA"], "event": os.environ["GITHUB_EVENT_NAME"],
                             "branch": os.environ["GITHUB_REF_NAME"], "per_page": 20},
                     headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                              "Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    return r.json().get("workflow_runs", [])


def wait(workflow, appear=300, finish=6 * 3600):
    start = time.time()
    while True:
        found = runs(workflow)
        if found:
            break
        if time.time() - start > appear:
            sys.exit(f"{workflow}: no run for commit {os.environ['GITHUB_SHA'][:7]} after {appear // 60} minutes")
        time.sleep(15)
    run = max(found, key=lambda x: x["run_attempt"] * 10**12 + x["id"])   # the latest attempt of the newest run
    print(f"waiting for {workflow}: {run['html_url']}")
    while run["status"] != "completed":
        if time.time() - start > finish:
            sys.exit(f"{workflow}: still {run['status']} after {finish // 3600} hours")
        time.sleep(30)
        run = next((x for x in runs(workflow) if x["id"] == run["id"]), run)
    if run["conclusion"] != "success":
        sys.exit(f"{workflow} finished {run['conclusion']}: {run['html_url']} (fix it and re-run; this pipeline "
                 "deploys only on top of it)")
    print(f"{workflow}: succeeded")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for w in sys.argv[1:]:
        wait(w)
