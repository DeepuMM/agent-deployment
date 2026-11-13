"""Smoke tests for the model pipelines, run after terraform apply.

  model_smoke.py models  [environment]             every model in the gateway registry: deployment healthy, backend
                                                   registered, kill switch state
  model_smoke.py access  <project> [environment]   the project's agents can use each allowed model through the
                                                   gateway, and nobody else can call the project's model API

Everything is read from Azure (the registry from APIM named values model-meta-*), not from Git: the test proves
what is live. `access` creates a temporary prompt agent model-access-smoke in the project that uses
"<connection>/<model>", runs the model's tests.yaml through it, and deletes it.

Sign-in: Azure CLI (az login locally, azure/login in GitHub Actions). The identity needs Reader on APIM and the
hosting accounts, and Foundry User on the project.
"""
import glob
import json
import os
import sys

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARM = "https://management.azure.com"
SMOKE_AGENT = "model-access-smoke"
cred = AzureCliCredential(process_timeout=60)


def environment(name):
    with open(os.path.join(ROOT, "platform", "environments", f"{name}.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    print(text)


def arm_get(path, api_version):
    token = cred.get_token(f"{ARM}/.default").token
    r = requests.get(f"{ARM}{path}", params={"api-version": api_version},
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    return r


def apim_path(env):
    g = env["gateway"]
    return (f"/subscriptions/{g['subscription_id']}/resourceGroups/{g['resource_group']}"
            f"/providers/Microsoft.ApiManagement/service/{g['apim_name']}")


def named_values(env):
    r = arm_get(f"{apim_path(env)}/namedValues", "2024-05-01")
    if r.status_code != 200:
        sys.exit(f"Can't read the gateway's named values: {r.status_code} {r.text[:300]}")
    return {nv["name"]: nv["properties"].get("value") for nv in r.json()["value"]}


def registry(env):
    return {json.loads(v)["name"]: json.loads(v) for n, v in named_values(env).items() if n.startswith("model-meta-")}


def model_tests(model):
    found = glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", "*", "models", model, "tests.yaml"))
    if not found:
        return [{"input": "Reply with the single word OK.", "must_contain_any": ["OK"]}]
    with open(found[0], encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ----------------------------------------------------------------------------- models
def smoke_models(env):
    values = named_values(env)
    reg = {json.loads(v)["name"]: json.loads(v) for n, v in values.items() if n.startswith("model-meta-")}
    if not reg:
        sys.exit("The gateway registry is empty: no model-meta-* named values.")
    failed = []
    rows = ["| Model | Hosting account | Deployment | Backend | Kill switch |", "|---|---|---|---|---|"]
    for name, m in sorted(reg.items()):
        acct = next((a for a in glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", m["account"], "account.yaml"))), None)
        rg = yaml.safe_load(open(acct, encoding="utf-8"))["resource_group"] if acct else env["gateway"]["resource_group"]
        sub = env["gateway"]["subscription_id"]
        dep = arm_get(f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/"
                      f"{m['account']}/deployments/{m['deployment']}", "2025-06-01")
        dep_state = dep.json().get("properties", {}).get("provisioningState") if dep.status_code == 200 else f"HTTP {dep.status_code}"
        be = arm_get(f"{apim_path(env)}/backends/{m['backend_id']}", "2024-05-01")
        be_state = "registered" if be.status_code == 200 else f"HTTP {be.status_code}"
        ks = values.get(m["kill_switch"], "missing")
        rows.append(f"| {name} | {m['account']} ({m['region']}) | {dep_state} | {be_state} | {ks} |")
        if dep_state != "Succeeded" or be_state != "registered" or ks not in ("active", "suspended"):
            failed.append(name)
    summary("\n".join(rows))
    if failed:
        sys.exit(f"Smoke test failed for: {', '.join(failed)}")


# ----------------------------------------------------------------------------- access
def smoke_access(env, project):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    with open(os.path.join(ROOT, "projects", project, "project.yaml"), encoding="utf-8") as fh:
        p = yaml.safe_load(fh)
    connection = p.get("models", {}).get("connection", "platform-models")
    allowed = [m["name"] for m in p.get("models", {}).get("allowed", [])]
    reg = registry(env)
    gateway_url = f"https://{env['gateway']['apim_name']}.azure-api.net/models/{project}"

    # 1. Nobody but the project's identity may call the project's model API (the pipeline's own token is refused).
    token = cred.get_token("https://cognitiveservices.azure.com/.default").token
    r = requests.post(f"{gateway_url}/deployments/{allowed[0]}/chat/completions", params={"api-version": "2024-10-21"},
                      headers={"Authorization": f"Bearer {token}"}, json={"messages": [{"role": "user", "content": "hi"}]},
                      timeout=60)
    other_refused = r.status_code == 401
    rows = [f"| Check | Result |", "|---|---|",
            f"| Another identity calling `{gateway_url}` | {'refused (401)' if other_refused else f'NOT refused ({r.status_code})'} |"]

    # 2. An agent in the project uses each allowed model through the connection.
    client = AIProjectClient(endpoint=p["foundry_project_endpoint"], credential=cred)
    openai = client.get_openai_client()
    failed = [] if other_refused else ["access control"]
    try:
        for model in allowed:
            if reg.get(model, {}).get("status") == "deprecated":
                rows.append(f"| `{model}` | deprecated: still served; move to a newer model |")
            client.agents.create_version(agent_name=SMOKE_AGENT, definition=PromptAgentDefinition(
                model=f"{connection}/{model}", instructions="You are a smoke test. Follow the user's instruction exactly."))
            for t in model_tests(model):
                resp = openai.responses.create(input=t["input"],
                                               extra_body={"agent_reference": {"name": SMOKE_AGENT, "type": "agent_reference"}})
                ok = any(w.lower() in resp.output_text.lower() for w in t["must_contain_any"])
                rows.append(f"| Agent with `{connection}/{model}`: \"{t['input']}\" | {'pass' if ok else 'FAIL'}: {resp.output_text[:80]!r} |")
                if not ok:
                    failed.append(model)
    finally:
        try:
            client.agents.delete(agent_name=SMOKE_AGENT)
        except Exception as e:  # nothing to delete when the first create failed
            print(f"  (cleanup: {e})")
    summary(f"**Model access smoke test, project {project}**\n\n" + "\n".join(rows))
    if failed:
        sys.exit(f"Smoke test failed: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("models", "access"):
        sys.exit(__doc__)
    if sys.argv[1] == "models":
        smoke_models(environment(sys.argv[2] if len(sys.argv) > 2 else "test"))
    else:
        if len(sys.argv) < 3:
            sys.exit("usage: model_smoke.py access <project> [environment]")
        smoke_access(environment(sys.argv[3] if len(sys.argv) > 3 else "test"), sys.argv[2])
