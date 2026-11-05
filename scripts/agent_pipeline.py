"""Agent deployment pipeline: Foundry prompt agents, published per project on the AI gateway (APIM).

One Foundry project = one APIM API (path from project.yaml); each agent in the project = one operation
(POST /<agent>/responses). Agent docs become operation descriptions; project docs become the API description.

  agent_pipeline.py validate [project ...]   offline checks of projects/<project>/ (no Azure access)
  agent_pipeline.py deploy   <project>       create a new Foundry version for each agent whose definition changed
  agent_pipeline.py evaluate <project>       Foundry evaluation of each new version; fails the run below the threshold
  agent_pipeline.py register <project>       named values (incl. pinned version), project API, policies, product, portal
  agent_pipeline.py smoke    <project>       run every agent's tests.yaml through the gateway
  agent_pipeline.py evaluate-versions <project> <agent> <version> ...   compare versions (no gateway change)

The gateway calls each agent at a pinned version (named value agent-version-<agent>), which register only moves
to a version that deploy created and evaluate passed in the same run, or to the current version when nothing changed.

Environment: AZURE_SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_NAME. The Foundry project endpoint is in project.yaml.
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
PROJECTS = os.path.join(ROOT, "projects")
STATE = os.path.join(ROOT, ".pipeline")          # versions and evaluation results passed between steps
EVALUATORS = {"intent_resolution", "task_adherence", "relevance", "coherence", "fluency", "groundedness"}
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ARM_API = "2022-08-01"
cred = AzureCliCredential(process_timeout=60)


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return value


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n\n")
    print(text)


# ----------------------------------------------------------------------------- definitions (Git)
def read(folder, name):
    return open(os.path.join(folder, name), encoding="utf-8").read()


def load_agent(folder):
    a = yaml.safe_load(read(folder, "agent.yaml"))
    a["folder"] = os.path.basename(folder)
    a["instructions"] = read(folder, a["instructions_file"])
    a["docs"] = read(folder, a["docs_file"])
    a["tests"] = yaml.safe_load(read(folder, a["tests_file"])) or []
    a["evals"] = yaml.safe_load(read(folder, a["evals_file"])) if a.get("evals_file") else None
    a["definition_sha256"] = hashlib.sha256(json.dumps(
        {"model": a["model"], "instructions": a["instructions"], "description": a["description"]},
        sort_keys=True).encode()).hexdigest()
    return a


def load_project(name):
    folder = os.path.join(PROJECTS, name)
    p = yaml.safe_load(read(folder, "project.yaml"))
    p["folder"] = name
    p["docs"] = read(folder, p["docs_file"])
    p["agents"] = [load_agent(f) for f in sorted(glob.glob(os.path.join(folder, "agents", "*"))) if os.path.isdir(f)]
    return p


def validate(names):
    errors = []
    for n in names:
        try:
            p = load_project(n)
        except Exception as e:  # missing file, bad YAML
            errors.append(f"{n}: {e}")
            continue
        if p.get("name") != n or not NAME_RE.match(n):
            errors.append(f"{n}: name must match the folder and use lowercase letters, digits and hyphens")
        for key in ("display_name", "owner", "foundry_project_endpoint", "gateway"):
            if not p.get(key):
                errors.append(f"{n}: '{key}' is required")
        g = p.get("gateway") or {}
        if not str(g.get("path", "")).startswith("ai/"):
            errors.append(f"{n}: gateway.path must start with ai/")
        if not p["agents"]:
            errors.append(f"{n}: the project has no agents")
        for a in p["agents"]:
            an = a["folder"]
            if a.get("name") != an or not NAME_RE.match(an):
                errors.append(f"{n}/{an}: name must match the folder and use lowercase letters, digits and hyphens")
            for key in ("display_name", "description", "owner", "model"):
                if not a.get(key):
                    errors.append(f"{n}/{an}: '{key}' is required")
            if len(a.get("instructions", "").strip()) < 20:
                errors.append(f"{n}/{an}: instructions are empty or too short")
            if not a.get("tests"):
                errors.append(f"{n}/{an}: at least one smoke test is required")
            ev = a.get("evals") or {}
            if not ev:
                errors.append(f"{n}/{an}: evals_file with a Foundry evaluation is required")
            else:
                if len(ev.get("dataset") or []) < 3:
                    errors.append(f"{n}/{an}: the evaluation dataset needs at least 3 queries")
                if not set(ev.get("evaluators") or []) <= EVALUATORS or not ev.get("evaluators"):
                    errors.append(f"{n}/{an}: evaluators must be from {sorted(EVALUATORS)}")
                if not 0 < float(ev.get("min_pass_rate", 0)) <= 1 or not ev.get("judge_model"):
                    errors.append(f"{n}/{an}: judge_model and min_pass_rate (0-1] are required")
        for scope, lim in [(n, g)] + [(f"{n}/{a['folder']}", a.get("gateway") or {}) for a in p["agents"]]:
            for key in ("rate_limit_per_minute", "quota_per_day"):
                if not isinstance(lim.get(key), int) or lim[key] <= 0:
                    errors.append(f"{scope}: gateway.{key} must be a positive integer")
        print(f"{n}: {len(p['agents'])} agent(s) checked")
    if errors:
        sys.exit("Validation failed:\n  " + "\n  ".join(errors))
    print(f"OK: {len(names)} project(s)")


# ----------------------------------------------------------------------------- Foundry
def deploy(name):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition
    p = load_project(name)
    project = AIProjectClient(endpoint=p["foundry_project_endpoint"], credential=cred)
    state = {}
    for a in p["agents"]:
        served = served_version(a["name"])
        try:
            versions = list(project.agents.list_versions(a["name"]))
        except Exception:
            versions = []
        latest = max(versions, key=lambda v: int(v.version)) if versions else None
        if latest and (latest.metadata or {}).get("definition_sha256") == a["definition_sha256"]:
            approved = served == str(latest.version)
            summary(f"**{a['name']}**: Foundry definition unchanged, version {latest.version}"
                    + ("" if approved else f" (not yet approved; gateway serves {served or 'none'})"))
            state[a["name"]] = {"version": str(latest.version), "new": not approved}
            continue
        v = project.agents.create_version(
            agent_name=a["name"], description=a["description"],
            definition=PromptAgentDefinition(model=a["model"], instructions=a["instructions"]),
            metadata={"definition_sha256": a["definition_sha256"], "owner": a["owner"],
                      "git_commit": os.environ.get("GITHUB_SHA", "local")[:40],
                      "deployed_by": "agent-deployment pipeline"})
        summary(f"**{a['name']}**: deployed Foundry agent version {v.version} (not live until it passes evaluation)")
        state[a["name"]] = {"version": str(v.version), "new": True}
    save_state(name, "versions", state)


def save_state(project, kind, data):
    os.makedirs(STATE, exist_ok=True)
    json.dump(data, open(os.path.join(STATE, f"{project}-{kind}.json"), "w"), indent=2)


def load_state(project, kind):
    path = os.path.join(STATE, f"{project}-{kind}.json")
    return json.load(open(path)) if os.path.exists(path) else None


# ----------------------------------------------------------------------------- Foundry evaluation gate
def gate_criteria(a):
    ev = a["evals"]
    criteria = []
    for e in ev["evaluators"]:
        if e in ("intent_resolution", "task_adherence"):   # judge the whole run against the agent's instructions
            mapping = {"query": "{{item.instructed_query}}", "response": "{{sample.output_items}}"}
        elif e == "groundedness":                          # answer must be supported by the instructions
            mapping = {"query": "{{item.query}}", "response": "{{sample.output_text}}", "context": "{{item.context}}"}
        else:
            mapping = {"query": "{{item.query}}", "response": "{{sample.output_text}}"}
        criteria.append({
            "type": "azure_ai_evaluator", "name": e, "evaluator_name": f"builtin.{e}",
            "initialization_parameters": {"deployment_name": ev["judge_model"]}, "data_mapping": mapping})
    return criteria


ITEM_SCHEMA = {"type": "object", "required": ["query"], "properties": {
    "query": {"type": "string"}, "context": {"type": "string"},
    "instructed_query": {"type": "array", "items": {"type": "object"}}}}


def gate_evaluation(oai, a):
    """One Foundry evaluation per agent and evaluator set, so runs of every version can be compared in the portal."""
    criteria = gate_criteria(a)
    key = hashlib.sha256(json.dumps([criteria, ITEM_SCHEMA], sort_keys=True).encode()).hexdigest()[:12]
    for e in oai.evals.list(limit=100):
        if (e.metadata or {}).get("agent") == a["name"] and (e.metadata or {}).get("gate_key") == key:
            return e
    return oai.evals.create(
        name=f"{a['name']} release gate", metadata={"agent": a["name"], "gate_key": key, "pipeline": "agent-deployment"},
        data_source_config={"type": "custom", "include_sample_schema": True, "item_schema": ITEM_SCHEMA},
        testing_criteria=criteria)


def run_gate(oai, a, version):
    """Evaluate one agent version; returns (passed, scores, report_url)."""
    ev = a["evals"]
    definition = gate_evaluation(oai, a)
    dataset_sha = hashlib.sha256(json.dumps(ev["dataset"], sort_keys=True).encode()).hexdigest()[:12]
    run = oai.evals.runs.create(
        eval_id=definition.id, name=f"{a['name']} v{version}",
        metadata={"agent_version": str(version), "dataset_sha": dataset_sha,
                  "git_commit": os.environ.get("GITHUB_SHA", "local")[:40], "pipeline": "agent-deployment"},
        data_source={"type": "azure_ai_target_completions",
                     "source": {"type": "file_content", "content": [{"item": {
                         "query": d["query"], "context": a["instructions"],
                         "instructed_query": [{"role": "system", "content": [{"type": "text", "text": a["instructions"]}]},
                                              {"role": "user", "content": [{"type": "text", "text": d["query"]}]}]}}
                         for d in ev["dataset"]]},
                     "input_messages": {"type": "template", "template": [{
                         "type": "message", "role": "user",
                         "content": {"type": "input_text", "text": "{{item.query}}"}}]},
                     "target": {"type": "azure_ai_agent", "name": a["name"], "version": str(version)}})
    print(f"{a['name']} v{version}: evaluation running ({len(ev['dataset'])} queries) in {definition.id}")
    end = time.time() + 1800
    while time.time() < end:
        r = oai.evals.runs.retrieve(run_id=run.id, eval_id=definition.id)
        if r.status in ("completed", "failed", "canceled"):
            break
        time.sleep(15)
    scores, ok = {}, r.status == "completed"
    for pc in (getattr(r, "per_testing_criteria_results", None) or []):
        total = pc.passed + pc.failed
        rate = pc.passed / total if total else 0.0
        scores[pc.testing_criteria] = {"passed": pc.passed, "total": total, "rate": round(rate, 2)}
        ok = ok and rate >= float(ev["min_pass_rate"])
    if set(scores) != set(ev["evaluators"]):
        ok = False   # an evaluator produced no result
    report = getattr(r, "report_url", None)
    line = ", ".join(f"{k} {s['passed']}/{s['total']}" for k, s in scores.items()) or f"status {r.status}"
    summary(f"**{a['name']} v{version}** evaluation {'PASSED' if ok else 'FAILED'} "
            f"(min pass rate {ev['min_pass_rate']:.0%}): {line}" + (f" · [report]({report})" if report else ""))
    return ok, scores, report


def evaluate(name):
    from azure.ai.projects import AIProjectClient
    p = load_project(name)
    versions = load_state(name, "versions") or sys.exit("Run deploy first: no versions recorded for this run")
    oai = AIProjectClient(endpoint=p["foundry_project_endpoint"], credential=cred).get_openai_client()
    results, failed = {}, []
    for a in p["agents"]:
        v = versions[a["name"]]
        if not v["new"]:
            print(f"{a['name']}: version {v['version']} already approved, evaluation not needed")
            continue
        ok, scores, report = run_gate(oai, a, v["version"])
        results[a["name"]] = {"version": v["version"], "passed": ok, "scores": scores, "report": report}
        if not ok:
            failed.append(a["name"])
    save_state(name, "evals", results)
    if failed:
        sys.exit(f"Evaluation gate failed for: {', '.join(failed)}. The gateway keeps serving the previous versions.")


# ----------------------------------------------------------------------------- APIM (ARM REST)
def arm_base():
    return (f"https://management.azure.com/subscriptions/{env('AZURE_SUBSCRIPTION_ID')}/resourceGroups/"
            f"{env('APIM_RESOURCE_GROUP')}/providers/Microsoft.ApiManagement/service/{env('APIM_NAME')}")


def arm(method, path, body=None, ok=(200, 201, 202, 204), params=None):
    token = cred.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    if method in ("PUT", "PATCH"):
        headers.update({"Content-Type": "application/json", "If-Match": "*"})
    r = requests.request(method, f"{arm_base()}/{path}", params={"api-version": ARM_API, **(params or {})},
                         headers=headers, json=body, timeout=120)
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


def served_version(agent):
    """The agent version the gateway serves (named value agent-version-<agent>), or None before the first release."""
    r = arm("POST", f"namedValues/agent-version-{agent}/listValue", ok=(200, 404))
    return r.json()["value"] if r.status_code == 200 else None


def put_named_value(name, value, only_if_missing=False):
    path = f"namedValues/{name}"
    if only_if_missing and arm("GET", path, ok=(200, 404)).status_code == 200:
        print(f"  named value {name}: exists, left unchanged")
        return
    arm("PUT", path, {"properties": {"displayName": name, "value": value, "secret": False}})
    wait_for(path, lambda b: b["properties"].get("provisioningState", "Succeeded") == "Succeeded")
    print(f"  named value {name} = {value}")


def openapi_spec(p):
    paths = {}
    for a in p["agents"]:
        g = a["gateway"]
        paths[f"/{a['name']}/responses"] = {"post": {
            "operationId": f"invoke-{a['name']}",
            "summary": a["display_name"],
            "description": a["docs"] + (f"\n\n**Agent limits per subscription:** {g['rate_limit_per_minute']} calls a "
                                        f"minute, {g['quota_per_day']:,} a day."),
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["input"], "properties": {
                    "input": {"type": "string", "description": "The user's question"},
                    "conversation": {"type": "string", "description": "Optional conversation ID for multi-turn"}}},
                "example": {"input": a["tests"][0]["input"]}}}},
            "responses": {
                "200": {"description": "The agent's answer (Responses API format)",
                        "content": {"application/json": {"example": {
                            "id": "resp_...", "status": "completed",
                            "agent_reference": {"type": "agent_reference", "name": a["name"]},
                            "output": [{"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "..."}]}]}}}},
                "401": {"description": "Missing or invalid subscription key"},
                "429": {"description": "Project or agent limit exceeded; see Retry-After"},
                "503": {"description": "Agent suspended by the kill switch (AI_ROUTE_SUSPENDED)"}}}}
    return {
        "openapi": "3.0.1",
        "info": {"title": f"AI project - {p['display_name']}", "version": "1.0", "description": p["docs"]},
        "servers": [{"url": p["foundry_project_endpoint"]}],
        "paths": paths,
        "components": {"securitySchemes": {"subscriptionKey": {
            "type": "apiKey", "in": "header", "name": "Ocp-Apim-Subscription-Key"}}},
        "security": [{"subscriptionKey": []}],
    }


def template(name, **values):
    text = read(os.path.join(ROOT, "templates"), name)
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    return text


def ensure_product(product):
    if arm("GET", f"products/{product}", ok=(200, 404)).status_code == 404:
        arm("PUT", f"products/{product}", {"properties": {
            "displayName": product, "description": "Agent APIs", "subscriptionRequired": True,
            "approvalRequired": False, "state": "published"}})
        print(f"  product {product}: created")
    arm("PUT", f"products/{product}/groups/developers")   # signed-in developers can see the APIs and docs


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
    p = load_project(name)
    g = p["gateway"]
    api_id = f"project-{name}"
    print(f"Registering project {name} in APIM {env('APIM_NAME')} as API {api_id}")
    versions = load_state(name, "versions") or sys.exit("Run deploy first: no versions recorded for this run")
    evals = load_state(name, "evals") or {}
    for a in p["agents"]:
        v = versions[a["name"]]
        if v["new"] and not (evals.get(a["name"]) or {}).get("passed"):
            sys.exit(f"{a['name']} v{v['version']} has not passed evaluation in this run; not switching the gateway")
        put_named_value(f"agent-name-{a['name']}", a["name"])
        put_named_value(f"agent-version-{a['name']}", v["version"])                # the version the gateway serves
        put_named_value(f"ks-agent-{a['name']}", "active", only_if_missing=True)   # never re-enable a suspended agent

    arm("PUT", f"apis/{api_id}", {"properties": {
        "format": "openapi+json", "value": json.dumps(openapi_spec(p)), "path": g["path"],
        "protocols": ["https"], "subscriptionRequired": True}}, params={"import": "true"})
    wait_for(f"apis/{api_id}")
    arm("PATCH", f"apis/{api_id}", {"properties": {"serviceUrl": p["foundry_project_endpoint"],
                                                   "subscriptionRequired": True}})
    print(f"  API {api_id} imported at /{g['path']} with {len(p['agents'])} agent operation(s)")

    arm("PUT", f"apis/{api_id}/policies/policy", {"properties": {"format": "rawxml", "value": template(
        "project-policy.xml", RATE=g["rate_limit_per_minute"], QUOTA=g["quota_per_day"])}})
    for a in p["agents"]:
        ag = a["gateway"]
        arm("PUT", f"apis/{api_id}/operations/invoke-{a['name']}/policies/policy",
            {"properties": {"format": "rawxml", "value": template(
                "agent-operation-policy.xml", AGENT=a["name"], RATE=ag["rate_limit_per_minute"],
                QUOTA=ag["quota_per_day"])}})
    print("  project and agent policies applied")

    ensure_product(g["product"])
    arm("PUT", f"products/{g['product']}/apis/{api_id}")
    print(f"  added to product {g['product']} (visible to the Developers group)")

    publish_portal(f"{name} {os.environ.get('GITHUB_SHA', 'local')[:7]}")
    base = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}"
    summary(f"**{name}**: API `{base}/{{agent}}/responses` · docs "
            f"https://{env('APIM_NAME')}.developer.azure-api.net/api-details#api={api_id}")


# ----------------------------------------------------------------------------- smoke test through the gateway
def smoke(name):
    p = load_project(name)
    g = p["gateway"]
    sub = "pipeline-smoke-test"
    if arm("GET", f"subscriptions/{sub}", ok=(200, 404)).status_code == 404:
        arm("PUT", f"subscriptions/{sub}", {"properties": {
            "displayName": sub, "scope": f"/products/{g['product']}", "state": "active"}})
    key = arm("POST", f"subscriptions/{sub}/listSecrets").json()["primaryKey"]
    failures, ran = [], 0
    for a in p["agents"]:
        ks = arm("POST", f"namedValues/ks-agent-{a['name']}/listValue").json()["value"]
        if ks.lower() != "active":
            summary(f"**{a['name']}**: kill switch is '{ks}', smoke test skipped")
            continue
        url = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}/{a['name']}/responses"
        for t in a["tests"]:
            for attempt in range(6):   # a new subscription key or API can take a minute to reach the gateway
                r = requests.post(url, headers={"Ocp-Apim-Subscription-Key": key}, json={"input": t["input"]},
                                  timeout=180)
                if r.status_code not in (401, 404):
                    break
                time.sleep(15)
            text = ""
            if r.status_code == 200:
                for item in r.json().get("output", []):
                    if item.get("type") == "message":
                        text += " ".join(c.get("text", "") for c in item.get("content", []))
            ok = r.status_code == 200 and any(w.lower() in text.lower() for w in t["must_contain_any"])
            ran += 1
            print(f"  {'PASS' if ok else 'FAIL'}  HTTP {r.status_code}  {a['name']}: {t['input'][:55]}")
            if not ok:
                failures.append(f"{a['name']}: {t['input'][:45]}: HTTP {r.status_code}, "
                                f"answer: {text[:200] or r.text[:200]}")
    if failures:
        sys.exit("Smoke test failed:\n  " + "\n  ".join(failures))
    summary(f"**{name}**: smoke test passed ({ran} checks through the gateway)")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "validate":
        validate(args or sorted(os.path.basename(x) for x in glob.glob(os.path.join(PROJECTS, "*")) if os.path.isdir(x)))
    elif cmd == "evaluate-versions":   # evaluate-versions <project> <agent> <version> ... (comparison only)
        from azure.ai.projects import AIProjectClient
        proj = load_project(args[0])
        agent = next(x for x in proj["agents"] if x["name"] == args[1])
        client = AIProjectClient(endpoint=proj["foundry_project_endpoint"], credential=cred).get_openai_client()
        for ver in args[2:]:
            run_gate(client, agent, ver)
    elif cmd in ("deploy", "evaluate", "register", "smoke"):
        {"deploy": deploy, "evaluate": evaluate, "register": register, "smoke": smoke}[cmd](args[0])
    else:
        sys.exit(__doc__)
