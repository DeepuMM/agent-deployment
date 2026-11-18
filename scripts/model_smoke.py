"""Smoke test for use-case onboarding, run after terraform apply.

  model_smoke.py access <project> [environment]   the project's agents can use each allowed model through the gateway,
                                                  nobody else can call the project's model API, and the project's own
                                                  content safety settings are the ones the gateway applies

Everything is read from Azure (the model registry from APIM named values model-meta-*), not from Git: the test proves
what is live. It creates a temporary prompt agent model-project-connection-smoke-<run> in the use case's Foundry
project that uses "<connection>/<model>", asks it to reply OK, and deletes it. Model checks (deployment, backend, kill
switch) are the ai-gateway repository's model smoke test.

Content safety proof: a fixed prompt with a known Violence severity (CS_PROBE) goes through the same agent. With the
project's effective violence threshold (model setting, else project setting, else the platform default 4) the
gateway must block it (severity >= threshold) or let it through (severity < threshold). Two projects with different
settings therefore get opposite results for the same prompt.

Sign-in: Azure CLI (az login locally, azure/login in GitHub Actions). The identity needs Reader on APIM and Foundry
User on the project.
"""
import glob
import json
import os
import sys

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM = "https://management.azure.com"
# One name per pipeline run: runs that smoke-test the same project at the same time (e.g. two onboarding
# runs after one merge) must not create and delete each other's temporary agent.
SMOKE_AGENT = (f"model-project-connection-smoke-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
               if os.environ.get("GITHUB_RUN_ID") else f"model-project-connection-smoke-local-{os.getpid()}")
cred = AzureCliCredential(process_timeout=60)

# Content safety proof prompt: Azure AI Content Safety rates it Violence severity CS_PROBE["severity"] (EightSeverityLevels),
# every other category 0. Calibrated against the Content Safety API; recheck if Azure changes its scoring.
CS_PROBE = {"input": "A soldier was wounded by gunfire during the battle.", "category": "violence", "severity": 3}
CS_DEFAULT = 4                      # platform default per category (shared fragment model-content-safety)


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


def cs_threshold(project_yaml, model):
    """Effective threshold of the probe's category for one model: model setting, else project, else platform default."""
    models = project_yaml.get("models", {})
    m = next((a for a in models.get("allowed", []) if a["name"] == model), {})
    for scope in (m.get("content_safety") or {}, models.get("content_safety") or {}):
        if CS_PROBE["category"] in scope:
            return int(scope[CS_PROBE["category"]])
    return CS_DEFAULT


def ask(openai, text):
    """The agent's answer, or ("blocked", error) when the gateway refuses the model call."""
    try:
        resp = openai.responses.create(input=text, extra_body={"agent_reference": {"name": SMOKE_AGENT, "type": "agent_reference"}})
    except Exception as e:  # the model call failed inside the agent run: the gateway's 403 surfaces here
        return "blocked", str(e)[:200]
    if getattr(resp, "status", "completed") == "failed" or getattr(resp, "error", None):
        return "blocked", str(getattr(resp, "error", ""))[:200]
    return "answered", resp.output_text[:80]


def model_tests(model):
    # A model's own tests live in the ai-gateway repository; here the default access check is used.
    found = glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", "*", "models", model, "tests.yaml"))
    if not found:
        return [{"input": "Reply with the single word OK.", "must_contain_any": ["OK"]}]
    with open(found[0], encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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
    # The use case's Foundry project (foundry.project, default: the folder name), created by use-case onboarding.
    endpoint = (f"https://{env['agents_foundry']['account_name']}.services.ai.azure.com/api/projects/"
                f"{(p.get('foundry') or {}).get('project', project)}")
    client = AIProjectClient(endpoint=endpoint, credential=cred)
    openai = client.get_openai_client()
    failed = [] if other_refused else ["access control"]
    gateway_switch = (named_values(env).get("ks-gateway-all") or "active").lower()
    if gateway_switch != "active":
        rows.append(f"| Agents through the gateway | skipped: global kill switch `ks-gateway-all` is '{gateway_switch}' |")
    try:
        for model in (allowed if gateway_switch == "active" else []):
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
            # Content safety proof: the project's own threshold decides.
            threshold = cs_threshold(p, model)
            expect = "blocked" if CS_PROBE["severity"] >= threshold else "answered"
            for attempt in range(3):  # Content Safety fails closed when unavailable: retry an unexpected block
                got, detail = ask(openai, CS_PROBE["input"])
                if got == expect or got == "answered":
                    break
            rows.append(f"| Content safety proof with `{model}`: {CS_PROBE['category']} severity {CS_PROBE['severity']}, "
                        f"project threshold {threshold} | expected {expect}, got {got}"
                        f"{'' if got == expect else ' (FAIL)'}: {detail!r} |")
            if got != expect:
                failed.append(f"{model} content safety")
    finally:
        try:
            client.agents.delete(agent_name=SMOKE_AGENT)
        except Exception as e:  # nothing to delete when the first create failed
            print(f"  (cleanup: {e})")
    summary(f"**Model project connection smoke test, project {project}**\n\n" + "\n".join(rows))
    if failed:
        sys.exit(f"Smoke test failed: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "access":
        sys.exit("usage: model_smoke.py access <project> [environment]   (model checks: the ai-gateway repository)")
    smoke_access(environment(sys.argv[3] if len(sys.argv) > 3 else "test"), sys.argv[2])
