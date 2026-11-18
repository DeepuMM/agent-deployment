"""Agent deployment pipeline: Foundry agents, published per project on the AI gateway (APIM).

Two kinds of agent (agent.yaml `kind`): prompt (default; a model + instructions) and hosted (code: the pipeline zips
`code.source_dir`, Foundry builds and runs it). Both go through the same steps and the same gateway contract.

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
Hosted agents are served by their own Foundry endpoint, which takes no version in the request: there the version is
pinned in Foundry (the endpoint's version selector, 100% to the approved version). deploy pins it to the served
version BEFORE uploading, because a new upload would otherwise go live at once (the default selector is @latest).

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
INSTRUCTION_EVALUATORS = {"intent_resolution", "task_adherence", "groundedness"}   # need agent.yaml instructions_file
KINDS = {"prompt", "hosted"}
HOSTED_RUNTIMES = {"python_3_11", "python_3_12", "python_3_13"}
HOSTED_SIZES = {("0.25", "0.5Gi"), ("0.5", "1Gi"), ("1", "2Gi"), ("2", "4Gi")}   # cpu, memory pairs
HOSTED_PROTOCOLS = {"responses", "invocations"}   # what the gateway route (fragment agent-route) passes through
ZIP_EXCLUDE = re.compile(r"(^|/)(\.env|\.venv|__pycache__|\.git|\.pytest_cache|.*\.pyc)(/|$)")
SECRET_NAME = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN|CONNECTION_STRING|CONNSTR)", re.I)
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


def code_zip(folder, source_dir):
    """The agent's code as a zip, byte-identical for identical files (sorted, fixed timestamps), so its hash only
    changes when the code does. Local-only files (.env, virtual environments, caches) are never uploaded."""
    import io
    import zipfile
    root = os.path.join(folder, source_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for f in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
                if ZIP_EXCLUDE.search(rel):
                    continue
                info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16   # regular file, rw-r--r-- (without the type bits extractors skip it)
                with open(os.path.join(dirpath, f), "rb") as fh:
                    z.writestr(info, fh.read())
    return buf.getvalue()


def hosted_environment(a):
    """Environment variables of a hosted agent: agent.yaml `environment` plus the model the code must use."""
    return {**{k: str(v) for k, v in (a.get("environment") or {}).items()},
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": a["model"]}


def load_agent(folder):
    a = yaml.safe_load(read(folder, "agent.yaml"))
    a["folder"] = os.path.basename(folder)
    a["kind"] = a.get("kind", "prompt")
    a["instructions"] = read(folder, a["instructions_file"]) if a.get("instructions_file") else ""
    a["docs"] = read(folder, a["docs_file"])
    a["tests"] = yaml.safe_load(read(folder, a["tests_file"])) or []
    a["evals"] = yaml.safe_load(read(folder, a["evals_file"])) if a.get("evals_file") else None
    if a["kind"] == "hosted":
        code = a.get("code") or {}
        a["code_zip"] = code_zip(folder, code["source_dir"]) if code.get("source_dir") and \
            os.path.isdir(os.path.join(folder, code["source_dir"])) else b""
        a["code_sha256"] = hashlib.sha256(a["code_zip"]).hexdigest()
        identity = {"kind": "hosted", "code_sha256": a["code_sha256"], "code": code, "cpu": a.get("cpu"),
                    "memory": a.get("memory"), "protocols": a.get("protocols"),
                    "environment": hosted_environment(a) if a.get("model") else {}, "description": a.get("description")}
    else:
        identity = {"model": a.get("model"), "instructions": a["instructions"], "description": a.get("description")}
    a["definition_sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return a


def platform_environment(name):
    return yaml.safe_load(read(os.path.join(ROOT, "platform", "environments"), f"{name}.yaml"))


def foundry_project(p):
    """The use case's Foundry project (foundry.project, default: the folder name), created by use-case onboarding."""
    return (p.get("foundry") or {}).get("project", p["folder"])


def load_project(name):
    folder = os.path.join(PROJECTS, name)
    p = yaml.safe_load(read(folder, "project.yaml"))
    p["folder"] = name
    # Endpoint of the Foundry project in the environment's agent account (onboarding's output foundry_project_endpoint).
    account = (platform_environment(p.get("environment", "test")).get("agents_foundry") or {}).get("account_name", "")
    p["foundry_project_endpoint"] = f"https://{account}.services.ai.azure.com/api/projects/{foundry_project(p)}"
    p["docs"] = read(folder, p["docs_file"])
    p["agents"] = [load_agent(f) for f in sorted(glob.glob(os.path.join(folder, "agents", "*"))) if os.path.isdir(f)]
    return p


# Content safety at the gateway (fragment model-content-safety): platform defaults. A project's
# models.content_safety may only be stricter. Keep in step with onboarding/terraform/locals.tf.
CONTENT_SAFETY_DEFAULTS = {"hate": 4, "sexual": 4, "self_harm": 4, "violence": 4, "prompt_shields": True,
                           "check_answers": False}   # check_answers: opt-in answer checks, off by default


def validate_models(n, p):
    """Connected models (README, use-case onboarding): when project.yaml has a models section, every agent and evaluation judge
    must use "<connection>/<allowed model>". Whether the model is registered and has capacity is checked against the
    gateway by the use-case-onboarding pipeline, which runs before deploy."""
    m = p.get("models")
    if not m:
        return []
    errors = []
    conn = m.get("connection", "platform-models")
    allowed = m.get("allowed") or []
    if not NAME_RE.match(conn):
        errors.append(f"{n}: models.connection must use lowercase letters, digits and hyphens")
    if not allowed:
        errors.append(f"{n}: models.allowed lists no models")
    for x in allowed:
        tpm, tpd = x.get("tokens_per_minute"), x.get("tokens_per_day")
        if not x.get("name") or not isinstance(tpm, int) or not isinstance(tpd, int) or tpm <= 0 or tpd < tpm:
            errors.append(f"{n}: models.allowed entry {x.get('name')!r} needs name, tokens_per_minute > 0 and "
                          "tokens_per_day >= tokens_per_minute")
    scopes = [("models.content_safety", m.get("content_safety"))] + [
        (f"models.allowed[{x.get('name')}].content_safety", x.get("content_safety")) for x in allowed]
    for scope, cs in scopes:
        if cs is None:
            continue
        if not isinstance(cs, dict):
            errors.append(f"{n}: {scope} must be a mapping")
            continue
        for k, v in cs.items():
            if k not in CONTENT_SAFETY_DEFAULTS:
                errors.append(f"{n}: {scope}.{k} is unknown; use {', '.join(CONTENT_SAFETY_DEFAULTS)}")
            elif k == "prompt_shields":
                if v is not True:
                    errors.append(f"{n}: {scope}.prompt_shields can only be true (it can't be turned off)")
            elif k == "check_answers":
                if not isinstance(v, bool):
                    errors.append(f"{n}: {scope}.check_answers must be true or false (default false)")
            elif isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= CONTENT_SAFETY_DEFAULTS[k]:
                errors.append(f"{n}: {scope}.{k} must be a whole number from 0 to {CONTENT_SAFETY_DEFAULTS[k]} "
                              "(platform default; lower blocks more, settings can only be stricter)")
    usable = {f"{conn}/{x.get('name')}" for x in allowed}
    for a in p["agents"]:
        if a.get("model") not in usable:
            errors.append(f"{n}/{a['folder']}: model {a.get('model')!r} must be one of {sorted(usable)} "
                          "(models are reached through the gateway connection)")
        judge = (a.get("evals") or {}).get("judge_model")
        if judge and judge not in usable:
            errors.append(f"{n}/{a['folder']}: evals judge_model {judge!r} must be one of {sorted(usable)}")
    return errors


def validate_foundry_project(n, p):
    """Each project has its own Foundry project (foundry.project, default: the folder name), created by use-case
    onboarding (onboarding/terraform), and its agents reach models only through the gateway,
    so models: is required."""
    errors = []
    raw = yaml.safe_load(read(os.path.join(PROJECTS, n), "project.yaml")) or {}
    if "foundry_project_endpoint" in raw:
        errors.append(f"{n}: foundry_project_endpoint is no longer used; set foundry.project (onboarding creates it)")
    fp = foundry_project(p)
    if not NAME_RE.match(fp):
        errors.append(f"{n}: foundry.project must use lowercase letters, digits and hyphens")
    if not (p.get("models") or {}).get("allowed"):
        errors.append(f"{n}: models.allowed is required (agents reach models only through the gateway)")
    conn = (p.get("models") or {}).get("connection", "platform-models")
    for f in sorted(glob.glob(os.path.join(PROJECTS, "*", "project.yaml"))):
        other = os.path.basename(os.path.dirname(f))
        q = yaml.safe_load(open(f, encoding="utf-8")) or {}
        if other == n:
            continue
        if (q.get("foundry") or {}).get("project", other) == fp:
            errors.append(f"{n}: Foundry project '{fp}' is also named by {other}; each project has its own")
        # Foundry connection names are unique per agent account, not per project.
        if (q.get("environment", "test") == p.get("environment", "test")
                and (q.get("models") or {}).get("connection", "platform-models") == conn):
            errors.append(f"{n}: models.connection '{conn}' is also used by {other} in environment "
                          f"{p.get('environment', 'test')}; connection names are unique per Foundry account")
    return errors


def validate_hosted(scope, a):
    """Offline checks of a hosted (code) agent: code present, supported runtime, size and protocol, no secrets."""
    errors, code = [], a.get("code") or {}
    if not a.get("code_zip"):
        errors.append(f"{scope}: code.source_dir must be a folder with the agent's code")
    else:
        import io
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(a["code_zip"])).namelist()
        entry = (code.get("entry_point") or [None, None])[-1]
        if not isinstance(code.get("entry_point"), list) or entry not in names:
            errors.append(f"{scope}: code.entry_point must be a command list whose last item is a file in "
                          f"{code.get('source_dir')} (e.g. [python, main.py])")
        if not {"pyproject.toml", "requirements.txt"} & set(names):
            errors.append(f"{scope}: {code.get('source_dir')} needs pyproject.toml or requirements.txt")
        if len(a["code_zip"]) > 250 * 1024 * 1024:
            errors.append(f"{scope}: the code zip is over Foundry's 250 MB limit")
    if code.get("runtime") not in HOSTED_RUNTIMES:
        errors.append(f"{scope}: code.runtime must be one of {sorted(HOSTED_RUNTIMES)}")
    if code.get("dependency_resolution", "remote_build") not in ("remote_build", "bundled"):
        errors.append(f"{scope}: code.dependency_resolution must be remote_build or bundled")
    if (str(a.get("cpu")), str(a.get("memory"))) not in HOSTED_SIZES:
        errors.append(f"{scope}: cpu/memory must be one of {sorted(HOSTED_SIZES)}")
    protocols = {p.get("protocol") for p in (a.get("protocols") or [])}
    if not protocols <= HOSTED_PROTOCOLS:
        errors.append(f"{scope}: protocols must be from {sorted(HOSTED_PROTOCOLS)} (what the gateway passes through)")
    if "responses" not in protocols:
        errors.append(f"{scope}: protocols must include responses: the evaluation gate and smoke tests use it")
    for k, v in (a.get("environment") or {}).items():
        if SECRET_NAME.search(k):
            errors.append(f"{scope}: environment.{k} looks like a secret; use Key Vault with the agent's identity")
    if "AZURE_AI_MODEL_DEPLOYMENT_NAME" in (a.get("environment") or {}):
        errors.append(f"{scope}: don't set AZURE_AI_MODEL_DEPLOYMENT_NAME; the pipeline sets it from `model`")
    return errors


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
        for key in ("display_name", "owner", "gateway"):
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
            if a["kind"] not in KINDS:
                errors.append(f"{n}/{an}: kind must be one of {sorted(KINDS)}")
            elif a["kind"] == "prompt" and len(a.get("instructions", "").strip()) < 20:
                errors.append(f"{n}/{an}: instructions are empty or too short")
            elif a["kind"] == "hosted":
                errors += validate_hosted(f"{n}/{an}", a)
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
                if not a.get("instructions") and set(ev.get("evaluators") or []) & INSTRUCTION_EVALUATORS:
                    errors.append(f"{n}/{an}: {sorted(INSTRUCTION_EVALUATORS)} judge against the agent's instructions:"
                                  " add instructions_file (for a hosted agent, the behaviour its code implements)")
                if not 0 < float(ev.get("min_pass_rate", 0)) <= 1 or not ev.get("judge_model"):
                    errors.append(f"{n}/{an}: judge_model and min_pass_rate (0-1] are required")
        errors += validate_models(n, p)
        errors += validate_foundry_project(n, p)
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
        metadata = {"definition_sha256": a["definition_sha256"], "owner": a["owner"],
                    "git_commit": os.environ.get("GITHUB_SHA", "local")[:40], "deployed_by": "agent-deployment pipeline"}
        if a["kind"] == "hosted":
            v = deploy_hosted(p, project, a, latest, served, metadata)
        else:
            v = project.agents.create_version(
                agent_name=a["name"], description=a["description"],
                definition=PromptAgentDefinition(model=a["model"], instructions=a["instructions"]), metadata=metadata)
        summary(f"**{a['name']}**: deployed Foundry agent version {v.version} (not live until it passes evaluation)")
        state[a["name"]] = {"version": str(v.version), "new": True}
    save_state(name, "versions", state)


def foundry_request(p, method, path, body=None, content_type="application/json"):
    token = cred.get_token("https://ai.azure.com/.default").token
    r = requests.request(method, f"{p['foundry_project_endpoint']}/{path}", params={"api-version": "v1"}, timeout=60,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                         data=json.dumps(body) if body is not None else None)
    if r.status_code >= 300:
        sys.exit(f"Foundry {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json() if r.text else {}


def pin_endpoint(p, agent, version):
    """Hosted agents: the agent's endpoint serves exactly this version (the default selector is @latest)."""
    foundry_request(p, "PATCH", f"agents/{agent}", {"agent_endpoint": {"version_selector": {"version_selection_rules": [
        {"type": "FixedRatio", "agent_version": str(version), "traffic_percentage": 100}]}}},
        content_type="application/merge-patch+json")
    rules = foundry_request(p, "GET", f"agents/{agent}")["agent_endpoint"]["version_selector"]["version_selection_rules"]
    if [(r["agent_version"], r["traffic_percentage"]) for r in rules] != [(str(version), 100)]:
        sys.exit(f"{agent}: endpoint version selector is {rules}, expected 100% to version {version}")


def deploy_hosted(p, project, a, latest, served, metadata):
    """Upload a hosted agent's code as a new version without making it live, and wait until Foundry has built it."""
    import io
    from azure.ai.projects.models import CodeConfiguration, HostedAgentDefinition, ProtocolVersionRecord
    # Keep serving what is live: the gateway's version, else the newest version that built (never a failed one).
    active = [x for x in project.agents.list_versions(a["name"]) if (x.get("status") or "") == "active"] if latest else []
    keep_live = served or (str(max(active, key=lambda x: int(x.version)).version) if active else None)
    if keep_live:   # the upload would otherwise go live at once (selector @latest)
        pin_endpoint(p, a["name"], keep_live)
    code = a["code"]
    stream = io.BytesIO(a["code_zip"])
    stream.name = f"{a['name']}.zip"
    v = project.agents.create_version_from_code(
        agent_name=a["name"], description=a["description"], metadata=metadata, code=stream,
        code_zip_sha256=a["code_sha256"],
        definition=HostedAgentDefinition(
            cpu=str(a["cpu"]), memory=str(a["memory"]), environment_variables=hosted_environment(a),
            protocol_versions=[ProtocolVersionRecord(protocol=x["protocol"], version=str(x["version"]))
                               for x in a["protocols"]],
            code_configuration=CodeConfiguration(
                runtime=code["runtime"], entry_point=[str(x) for x in code["entry_point"]],
                dependency_resolution=code.get("dependency_resolution", "remote_build"))))
    pin_endpoint(p, a["name"], keep_live or v.version)   # first version: nothing else to serve
    print(f"{a['name']} v{v.version}: code uploaded ({len(a['code_zip']):,} bytes), waiting for Foundry to build it")
    end = time.time() + 1200
    while time.time() < end:
        status = foundry_request(p, "GET", f"agents/{a['name']}/versions/{v.version}").get("status")
        if status == "active":
            break
        if status in ("failed", "deleted"):
            sys.exit(f"{a['name']} v{v.version}: Foundry build {status}; see the agent's logs in the Foundry portal")
        time.sleep(20)
    else:
        sys.exit(f"{a['name']} v{v.version}: not active after 20 minutes")
    print(f"{a['name']} v{v.version}: active (endpoint still serves version {keep_live or v.version})")
    return v


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
    elif method == "DELETE":
        headers["If-Match"] = "*"
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


METHODS = ("GET", "POST", "DELETE")


def agent_paths(a):
    """The Foundry-identical paths a caller uses after {gateway}/<project path>/<agent>/ (see fragment agent-route)."""
    if a["kind"] == "hosted":
        paths = ["POST endpoint/protocols/openai/responses?api-version=v1",
                 "POST endpoint/protocols/openai/conversations?api-version=v1 (then pass `conversation` to responses)",
                 "POST|GET endpoint/sessions?api-version=v1 (sessions; also GET|DELETE endpoint/sessions/{id}, POST endpoint/sessions/{id}:stop)"]
        if any(x.get("protocol") == "invocations" for x in a.get("protocols") or []):
            paths.append("POST endpoint/protocols/invocations?api-version=v1&agent_session_id={id} (the agent's own body format)")
        return paths
    return ["POST openai/v1/responses", "POST openai/v1/conversations (then pass `conversation` to responses)"]


def agent_operation_doc(p, a):
    g, base = a["gateway"], f"https://{env('APIM_NAME')}.azure-api.net/{p['gateway']['path']}/{a['name']}"
    def line(x):
        method, rest = x.split(" ", 1)
        path, _, note = rest.partition(" (")
        return f"- `{method} {base}/{path}`" + (f" ({note}" if note else "")
    usage = "\n".join(line(x) for x in agent_paths(a))
    return (a["docs"] + f"\n\n**Call it exactly as you would call Foundry**, with this base URL in place of the agent's "
            f"Foundry address, your Microsoft Entra token for `https://ai.azure.com` (`Authorization: Bearer`) and your "
            f"subscription key (`Ocp-Apim-Subscription-Key`):\n\n{usage}\n\nBody, query string, `traceparent` and "
            f"session/conversation IDs pass through; responses are Foundry's. The gateway serves the evaluated version "
            f"only. **Limits per subscription:** {g['rate_limit_per_minute']} calls a minute, {g['quota_per_day']:,} a day.")


def template(name, **values):
    text = read(os.path.join(ROOT, "templates"), name)
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    return text


def require_fragments(*templates):
    """The policy fragments the templates include must already be on this APIM. They are shared across every agent
    and deployed by the ai-gateway repository's gateway-policy-deployment pipeline, never by this pipeline."""
    ids = sorted({i for t in templates for i in re.findall(r'fragment-id="([^"]+)"', read(os.path.join(ROOT, "templates"), t))})
    missing = [i for i in ids if arm("GET", f"policyFragments/{i}", ok=(200, 404)).status_code == 404]
    if missing:
        sys.exit(f"Shared policy fragment(s) missing on APIM {env('APIM_NAME')}: {', '.join(missing)}. Run the "
                 "ai-gateway repository's gateway-policy-deployment workflow for this environment first.")


def ensure_product(product):
    if arm("GET", f"products/{product}", ok=(200, 404)).status_code == 404:
        arm("PUT", f"products/{product}", {"properties": {
            "displayName": product, "description": "Agent APIs", "subscriptionRequired": True,
            "approvalRequired": False, "state": "published"}})
        print(f"  product {product}: created")
    arm("PUT", f"products/{product}/groups/developers")   # signed-in developers can see the APIs and docs
    if os.environ.get("PORTAL_ANONYMOUS", "").lower() == "true":
        arm("PUT", f"products/{product}/groups/guests")   # portal without sign-in: anonymous visitors too


def publish_portal(note):
    for attempt in range(1, 11):   # concurrent publishes (other pipelines) get 429: retry
        rev = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        r = arm("PUT", f"portalRevisions/{rev}", {"properties": {"description": note[:100], "isCurrent": True}},
                ok=(200, 201, 202, 412, 429))
        if r.status_code != 429:
            break
        wait = int(r.headers.get("Retry-After", 30)) + 5 * attempt
        print(f"  another portal publish is in progress (429); retrying in {wait}s ({attempt}/10)")
        time.sleep(wait)
    if r.status_code == 429:
        summary("WARNING: Developer Portal publish still busy after 10 attempts; the API and its docs are registered "
                "and appear with the next publish.")
        return
    if r.status_code == 412:   # portal never opened in admin mode: Azure requires that once per APIM instance
        summary("WARNING: the Developer Portal is not provisioned yet. Open it once from the Azure portal "
                "(APIM > Developer portal) and re-run; the API and its docs are already registered.")
        return
    # Polls are throttled too when several pipelines publish at once: a 429 or an empty reply is just retried.
    # Docs never fail a deployment: the API and its docs are registered before this point.
    status, end = None, time.time() + 900
    while time.time() < end and status not in ("completed", "failed"):
        time.sleep(10)
        r = arm("GET", f"portalRevisions/{rev}", ok=(200, 429, 500, 502, 503))
        try:
            status = r.json()["properties"].get("status") if r.status_code == 200 else None
        except ValueError:
            status = None
    if status != "completed":
        summary(f"WARNING: Developer Portal revision {rev} did not complete ({status or 'timed out'}); the API and its "
                "docs are registered and appear with the next publish.")
        return
    print(f"  developer portal published (revision {rev})")


def register(name):
    p = load_project(name)
    g = p["gateway"]
    api_id = f"project-{name}"
    print(f"Registering project {name} in APIM {env('APIM_NAME')} as API {api_id}")
    require_fragments("project-policy.xml", "agent-operation-policy.xml")
    versions =load_state(name, "versions") or sys.exit("Run deploy first: no versions recorded for this run")
    evals = load_state(name, "evals") or {}
    for a in p["agents"]:
        v = versions[a["name"]]
        if v["new"] and not (evals.get(a["name"]) or {}).get("passed"):
            sys.exit(f"{a['name']} v{v['version']} has not passed evaluation in this run; not switching the gateway")
        if a["kind"] == "hosted":   # the hosted endpoint takes no version in the request: pin it in Foundry
            pin_endpoint(p, a["name"], v["version"])
        put_named_value(f"agent-name-{a['name']}", a["name"])
        put_named_value(f"agent-version-{a['name']}", v["version"])                # the version the gateway serves
        put_named_value(f"ks-agent-{a['name']}", "active", only_if_missing=True)   # never re-enable a suspended agent

    arm("PUT", f"apis/{api_id}", {"properties": {
        "displayName": f"AI project - {p['display_name']}", "description": p["docs"], "path": g["path"],
        "protocols": ["https"], "subscriptionRequired": True, "serviceUrl": p["foundry_project_endpoint"]}})
    wait_for(f"apis/{api_id}")
    # One operation per agent and method, on /<agent>/*: the caller's Foundry path follows (fragment agent-route).
    wanted = {f"{a['name']}-{m.lower()}": (a, m) for a in p["agents"] for m in METHODS}
    for op in arm("GET", f"apis/{api_id}/operations").json().get("value", []):
        if op["name"] not in wanted:   # e.g. the old POST /<agent>/responses operations, or a removed agent
            arm("DELETE", f"apis/{api_id}/operations/{op['name']}")
            print(f"  operation {op['name']}: removed")
    for op_id, (a, m) in wanted.items():
        arm("PUT", f"apis/{api_id}/operations/{op_id}", {"properties": {
            "displayName": f"{a['display_name']} ({m})", "method": m, "urlTemplate": f"/{a['name']}/*",
            "description": agent_operation_doc(p, a)}})
    print(f"  API {api_id} at /{g['path']}: {len(wanted)} operation(s), {len(p['agents'])} agent(s)")

    arm("PUT", f"apis/{api_id}/policies/policy", {"properties": {"format": "rawxml", "value": template(
        "project-policy.xml", RATE=g["rate_limit_per_minute"], QUOTA=g["quota_per_day"])}})
    for op_id, (a, m) in wanted.items():
        ag = a["gateway"]
        arm("PUT", f"apis/{api_id}/operations/{op_id}/policies/policy",
            {"properties": {"format": "rawxml", "value": template(
                "agent-operation-policy.xml", AGENT=a["name"], KIND=a["kind"], RATE=ag["rate_limit_per_minute"],
                QUOTA=ag["quota_per_day"])}})
    print("  project and agent policies applied")

    ensure_product(g["product"])
    arm("PUT", f"products/{g['product']}/apis/{api_id}")
    print(f"  added to product {g['product']} (visible to the Developers group)")

    publish_portal(f"{name} {os.environ.get('GITHUB_SHA', 'local')[:7]}")
    base = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}"
    summary(f"**{name}**: API `{base}/<agent>/` + the agent's Foundry path · docs "
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
    gw = arm("POST", "namedValues/ks-gateway-all/listValue", ok=(200, 404))
    if gw.status_code == 200 and gw.json()["value"].lower() != "active":
        summary(f"**{name}**: global kill switch ks-gateway-all is '{gw.json()['value']}', smoke test skipped")
        return
    failures, ran = [], 0
    for a in p["agents"]:
        ks = arm("POST", f"namedValues/ks-agent-{a['name']}/listValue").json()["value"]
        if ks.lower() != "active":
            summary(f"**{a['name']}**: kill switch is '{ks}', smoke test skipped")
            continue
        # The Foundry-identical path, the pipeline identity's own Entra token, and the gateway key (fragment agent-route).
        path = ("endpoint/protocols/openai/responses?api-version=v1" if a["kind"] == "hosted"
                else "openai/v1/responses")
        url = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}/{a['name']}/{path}"
        for t in a["tests"]:
            for attempt in range(6):   # a new subscription key or API can take a minute to reach the gateway
                headers = {"Ocp-Apim-Subscription-Key": key,
                           "Authorization": f"Bearer {cred.get_token('https://ai.azure.com/.default').token}"}
                r = requests.post(url, headers=headers, json={"input": t["input"]}, timeout=180)
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
