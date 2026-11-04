"""Agent deployment pipeline: Foundry prompt agent + AI gateway (APIM) registration + Developer Portal docs.

  agent_pipeline.py validate [agent ...]   offline checks of agents/<name>/ (no Azure access)
  agent_pipeline.py deploy   <agent>       create a new Foundry agent version if the definition changed
  agent_pipeline.py register <agent>       named values, API (OpenAPI with docs), policy, product, portal publish
  agent_pipeline.py smoke    <agent>       run tests.yaml through the gateway

Environment: FOUNDRY_PROJECT_ENDPOINT, AZURE_SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_NAME.
Azure sign-in comes from the Azure CLI (az login locally, azure/login in GitHub Actions).
"""
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, "agents")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ARM_API = "2022-08-01"
cred = AzureCliCredential(process_timeout=60)


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return value


def output(key, value):
    """Expose a value to later GitHub Actions steps."""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"{key}={value}\n")


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")
    print(text)


# ----------------------------------------------------------------------------- agent definition (Git)
def load(name):
    folder = os.path.join(AGENTS, name)
    a = yaml.safe_load(open(os.path.join(folder, "agent.yaml")))
    a["instructions"] = open(os.path.join(folder, a["instructions_file"]), encoding="utf-8").read()
    a["docs"] = open(os.path.join(folder, a["docs_file"]), encoding="utf-8").read()
    a["tests"] = yaml.safe_load(open(os.path.join(folder, a["tests_file"]))) or []
    a["definition_sha256"] = hashlib.sha256(json.dumps(
        {"model": a["model"], "instructions": a["instructions"], "description": a["description"]},
        sort_keys=True).encode()).hexdigest()
    return a


def validate(names):
    errors = []
    for n in names:
        try:
            a = load(n)
        except Exception as e:  # missing file, bad YAML
            errors.append(f"{n}: {e}")
            continue
        if a.get("name") != n or not NAME_RE.match(n):
            errors.append(f"{n}: name must match the folder and use lowercase letters, digits and hyphens")
        for key in ("display_name", "description", "owner", "model", "gateway"):
            if not a.get(key):
                errors.append(f"{n}: '{key}' is required")
        g = a.get("gateway") or {}
        if not str(g.get("path", "")).startswith("agents/"):
            errors.append(f"{n}: gateway.path must start with agents/")
        for key in ("rate_limit_per_minute", "quota_per_day"):
            if not isinstance(g.get(key), int) or g[key] <= 0:
                errors.append(f"{n}: gateway.{key} must be a positive integer")
        if len(a.get("instructions", "").strip()) < 20:
            errors.append(f"{n}: instructions are empty or too short")
        if not a.get("tests"):
            errors.append(f"{n}: at least one smoke test is required")
        print(f"{n}: checked")
    if errors:
        sys.exit("Validation failed:\n  " + "\n  ".join(errors))
    print(f"OK: {len(names)} agent(s)")


# ----------------------------------------------------------------------------- Foundry
def deploy(name):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition
    a = load(name)
    project = AIProjectClient(endpoint=env("FOUNDRY_PROJECT_ENDPOINT"), credential=cred)
    try:
        versions = list(project.agents.list_versions(name))
    except Exception:
        versions = []
    latest = max(versions, key=lambda v: int(v.version)) if versions else None
    if latest and (latest.metadata or {}).get("definition_sha256") == a["definition_sha256"]:
        summary(f"**{name}**: Foundry definition unchanged, staying on version {latest.version}")
        output("agent_version", latest.version)
        return
    v = project.agents.create_version(
        agent_name=name, description=a["description"],
        definition=PromptAgentDefinition(model=a["model"], instructions=a["instructions"]),
        metadata={"definition_sha256": a["definition_sha256"], "owner": a["owner"],
                  "git_commit": os.environ.get("GITHUB_SHA", "local")[:40],
                  "deployed_by": "agent-deployment pipeline"})
    summary(f"**{name}**: deployed Foundry agent version {v.version}")
    output("agent_version", v.version)


# ----------------------------------------------------------------------------- APIM (ARM REST)
def arm_base():
    return (f"https://management.azure.com/subscriptions/{env('AZURE_SUBSCRIPTION_ID')}/resourceGroups/"
            f"{env('APIM_RESOURCE_GROUP')}/providers/Microsoft.ApiManagement/service/{env('APIM_NAME')}")


def arm(method, path, body=None, ok=(200, 201, 202, 204), params=None):
    token = cred.get_token("https://management.azure.com/.default").token
    r = requests.request(method, f"{arm_base()}/{path}", params={"api-version": ARM_API, **(params or {})},
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                  "If-Match": "*"} if method in ("PUT", "PATCH") else
                         {"Authorization": f"Bearer {token}"},
                         json=body, timeout=120)
    if r.status_code not in ok:
        sys.exit(f"{method} {path} failed: {r.status_code} {r.text[:500]}")
    return r


def wait_for(path, check=lambda b: True, timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        r = arm("GET", path, ok=(200, 404))
        if r.status_code == 200 and check(r.json()):
            return r.json()
        time.sleep(5)
    sys.exit(f"Timed out waiting for {path}")


def put_named_value(name, value, only_if_missing=False):
    path = f"namedValues/{name}"
    if only_if_missing and arm("GET", path, ok=(200, 404)).status_code == 200:
        print(f"  named value {name}: exists, left unchanged")
        return
    arm("PUT", path, {"properties": {"displayName": name, "value": value, "secret": False}})
    wait_for(path, lambda b: b["properties"].get("provisioningState", "Succeeded") == "Succeeded")
    print(f"  named value {name} = {value}")


def openapi_spec(a):
    agent = a["name"]
    error = {"type": "object", "properties": {"error": {"type": "object", "properties": {
        "code": {"type": "string"}, "message": {"type": "string"}, "correlationId": {"type": "string"}}}}}
    return {
        "openapi": "3.0.1",
        "info": {"title": f"Agent - {a['display_name']}", "version": "1.0", "description": a["docs"]},
        "servers": [{"url": env("FOUNDRY_PROJECT_ENDPOINT")}],
        "paths": {"/responses": {"post": {
            "operationId": "invoke-agent",
            "summary": "Ask the agent",
            "description": (f"Sends a question to the **{a['display_name']}** agent and returns its answer. "
                            "Pass `conversation` to continue a multi-turn conversation."),
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["input"], "properties": {
                    "input": {"type": "string", "description": "The user's question"},
                    "conversation": {"type": "string", "description": "Optional conversation ID for multi-turn"}}},
                "example": {"input": a["tests"][0]["input"]}}}},
            "responses": {
                "200": {"description": "The agent's answer (Responses API format)",
                        "content": {"application/json": {"schema": {"type": "object", "properties": {
                            "id": {"type": "string"}, "status": {"type": "string"},
                            "agent_reference": {"type": "object"},
                            "output": {"type": "array", "items": {"type": "object"}}}},
                            "example": {"id": "resp_...", "status": "completed",
                                        "agent_reference": {"type": "agent_reference", "name": agent},
                                        "output": [{"type": "message", "role": "assistant", "content": [
                                            {"type": "output_text", "text": "..."}]}]}}}},
                "401": {"description": "Missing or invalid subscription key"},
                "429": {"description": "Rate limit or daily quota exceeded; see Retry-After"},
                "503": {"description": "Agent suspended by the kill switch (AI_ROUTE_SUSPENDED)",
                        "content": {"application/json": {"schema": error}}}}}}},
        "components": {"securitySchemes": {"subscriptionKey": {
            "type": "apiKey", "in": "header", "name": "Ocp-Apim-Subscription-Key"}}},
        "security": [{"subscriptionKey": []}],
    }


def ensure_product(product):
    if arm("GET", f"products/{product}", ok=(200, 404)).status_code == 404:
        arm("PUT", f"products/{product}", {"properties": {
            "displayName": product, "description": "Agent APIs", "subscriptionRequired": True,
            "approvalRequired": False, "state": "published"}})
        print(f"  product {product}: created")


def publish_portal(note):
    rev = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    r = arm("PUT", f"portalRevisions/{rev}", {"properties": {"description": note[:100], "isCurrent": True}},
            ok=(200, 201, 202, 412))
    if r.status_code == 412:   # portal never opened in admin mode: Azure requires that once per APIM instance
        summary("WARNING: the Developer Portal is not provisioned yet. Open it once from the Azure portal "
                "(APIM > Developer portal) and re-run; the API and its docs are already registered.")
        return
    b = wait_for(f"portalRevisions/{rev}", lambda b: b["properties"].get("status") in ("completed", "failed"), 900)
    if b["properties"]["status"] != "completed":
        sys.exit(f"Developer Portal publish failed: {b['properties'].get('statusDetails')}")
    print(f"  developer portal published (revision {rev})")


def register(name):
    a = load(name)
    g = a["gateway"]
    api_id = f"agent-{name}"
    print(f"Registering {name} in APIM {env('APIM_NAME')}")
    put_named_value(f"agent-name-{name}", name)
    put_named_value(f"ks-agent-{name}", "active", only_if_missing=True)   # never re-enable a suspended agent

    arm("PUT", f"apis/{api_id}", {"properties": {
        "format": "openapi+json", "value": json.dumps(openapi_spec(a)), "path": g["path"],
        "protocols": ["https"], "subscriptionRequired": True}}, params={"import": "true"})
    wait_for(f"apis/{api_id}")
    arm("PATCH", f"apis/{api_id}", {"properties": {"serviceUrl": env("FOUNDRY_PROJECT_ENDPOINT"),
                                                   "subscriptionRequired": True}})
    print(f"  API {api_id} imported at /{g['path']}")

    policy = open(os.path.join(ROOT, "templates", "agent-policy.xml"), encoding="utf-8").read()
    policy = (policy.replace("__AGENT__", name).replace("__RATE__", str(g["rate_limit_per_minute"]))
              .replace("__QUOTA__", str(g["quota_per_day"])))
    arm("PUT", f"apis/{api_id}/policies/policy", {"properties": {"format": "rawxml", "value": policy}})
    print("  policy applied")

    ensure_product(g["product"])
    arm("PUT", f"products/{g['product']}/apis/{api_id}")
    print(f"  added to product {g['product']}")

    publish_portal(f"{name} {os.environ.get('GITHUB_SHA', 'local')[:7]}")
    gateway = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}/responses"
    portal = f"https://{env('APIM_NAME')}.developer.azure-api.net/api-details#api={api_id}"
    summary(f"**{name}**: gateway `{gateway}` · docs {portal}")


# ----------------------------------------------------------------------------- smoke test through the gateway
def smoke(name):
    a = load(name)
    g = a["gateway"]
    ks = arm("POST", f"namedValues/ks-agent-{name}/listValue").json()["value"]
    if ks.lower() != "active":
        summary(f"**{name}**: kill switch is '{ks}', smoke test skipped")
        return
    sub = "pipeline-smoke-test"
    if arm("GET", f"subscriptions/{sub}", ok=(200, 404)).status_code == 404:
        arm("PUT", f"subscriptions/{sub}", {"properties": {
            "displayName": sub, "scope": f"/products/{g['product']}", "state": "active"}})
    key = arm("POST", f"subscriptions/{sub}/listSecrets").json()["primaryKey"]
    url = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}/responses"
    failures = []
    for t in a["tests"]:
        for attempt in range(6):   # a new subscription key can take a minute to reach the gateway
            r = requests.post(url, headers={"Ocp-Apim-Subscription-Key": key}, json={"input": t["input"]},
                              timeout=180)
            if r.status_code != 401:
                break
            time.sleep(15)
        text = ""
        if r.status_code == 200:
            for item in r.json().get("output", []):
                if item.get("type") == "message":
                    text += " ".join(c.get("text", "") for c in item.get("content", []))
        ok = r.status_code == 200 and any(w.lower() in text.lower() for w in t["must_contain_any"])
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {r.status_code}  {t['input'][:60]}")
        if not ok:
            failures.append(f"{t['input'][:50]}: HTTP {r.status_code}, answer: {text[:200] or r.text[:200]}")
    if failures:
        sys.exit("Smoke test failed:\n  " + "\n  ".join(failures))
    summary(f"**{name}**: smoke test passed ({len(a['tests'])} checks through the gateway)")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "validate":
        validate(args or sorted(os.path.basename(p) for p in glob.glob(os.path.join(AGENTS, "*")) if os.path.isdir(p)))
    elif cmd in ("deploy", "register", "smoke"):
        {"deploy": deploy, "register": register, "smoke": smoke}[cmd](args[0])
    else:
        sys.exit(__doc__)
