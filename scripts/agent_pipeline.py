"""Agent deployment pipeline: Foundry agents, published per project on the AI gateway (APIM).

Two kinds of agent (agent.yaml `kind`): prompt (default; a model + instructions) and hosted (code: the pipeline zips
`code.source_dir`, Foundry builds and runs it). Both go through the same steps and the same gateway contract.

One Foundry project = one APIM API (path from project.yaml); each agent in the project = one operation
(POST /<agent>/responses). Agent docs become operation descriptions; project docs become the API description.

  agent_pipeline.py validate [project ...]   offline checks of projects/<project>/ (no Azure access)
  agent_pipeline.py deploy   <project>       create a new Foundry version for each agent whose definition changed
  agent_pipeline.py evaluate <project>       Foundry evaluation of each new version; fails the run below the threshold
  agent_pipeline.py register <project>       named values (incl. pinned version), project API, policies, product,
                                             portal; lists the agent roles callers need
  agent_pipeline.py roles [project ...]      create the agent roles on the gateway's agent app (run by an owner of
                                             that app, not by the pipeline: the pipeline has no Entra rights)
  agent_pipeline.py smoke    <project>       run every agent's tests.yaml through the gateway
  agent_pipeline.py evaluate-versions <project> <agent> <version> ...   compare versions (no gateway change)

The gateway calls each agent at a pinned version (named value agent-version-<agent>), which register only moves
to a version that deploy created and evaluate passed in the same run, or to the current version when nothing changed.
Hosted agents are served by their own Foundry endpoint, which takes no version in the request: there the version is
pinned in Foundry (the endpoint's version selector, 100% to the approved version). deploy pins it to the served
version BEFORE uploading, because a new upload would otherwise go live at once (the default selector is @latest).

Who may call an agent: callers present a Microsoft Entra token for the gateway's agent app (AGENTS_APP_ID, set up once
per environment by the ai-gateway repository's scripts/setup_agents_app.py) holding the role agent.<project>.<agent>,
agent.<project>.all or agents.all (fragment agent-caller). An owner of that app creates the roles (`roles`) and
decides who holds them, outside the pipelines (README: Access to agents); the pipeline never changes Entra. No subscription key: limits count
per caller.

Environment (GitHub environment variables, see platform_env.py): AZURE_SUBSCRIPTION_ID, APIM_SUBSCRIPTION_ID,
APIM_RESOURCE_GROUP, APIM_NAME, FOUNDRY_ACCOUNT, AGENTS_APP_ID. The Foundry project is in project.yaml (foundry.project).
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
import uuid

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS = os.path.join(ROOT, "projects")
STATE = os.path.join(ROOT, ".pipeline")          # versions and evaluation results passed between steps
EVALUATORS = {"intent_resolution", "task_adherence", "relevance", "coherence", "fluency", "groundedness"}
INSTRUCTION_EVALUATORS = {"intent_resolution", "task_adherence", "groundedness"}   # need agent.yaml instructions_file
KINDS = {"prompt", "hosted"}
ACCESS = ("gateway", "foundry")                  # agent.yaml access: the checks a caller must pass (fragment agent-caller)
HOSTED_RUNTIMES = {"python_3_11", "python_3_12", "python_3_13"}
HOSTED_SIZES = {("0.25", "0.5Gi"), ("0.5", "1Gi"), ("1", "2Gi"), ("2", "4Gi")}   # cpu, memory pairs
HOSTED_PROTOCOLS = {"responses", "invocations"}   # what the gateway route (fragment agent-route) passes through
ZIP_EXCLUDE = re.compile(r"(^|/)(\.env|\.venv|__pycache__|\.git|\.pytest_cache|.*\.pyc)(/|$)")
SECRET_NAME = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN|CONNECTION_STRING|CONNSTR)", re.I)
TOOL_IMPACTS = {"read", "write"}                 # tool.yaml tools[].impact; write needs approval in agents
TOOL_APPROVALS = {"never", "always"}             # agent.yaml tools[].require_approval
TOOL_AUTH = {"none", "managed_identity"}         # tool.yaml backend.auth (managed_identity: granted by the tool's team)
TOOL_TEST_EXPECT = {"called", "refused"}         # tool tests.yaml expect
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


# Environment variables the pipeline sets in every hosted agent; an agent's own settings can't override them.
# (Foundry itself sets FOUNDRY_PROJECT_ENDPOINT and the agent's identity in the container.)
PLATFORM_ENV = {
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "the agent's model, <connection>/<model> (agent.yaml model)",
    "PLATFORM_ENVIRONMENT": "the environment the version is deployed to (dev, uat, prd)",
    "PLATFORM_PROJECT": "the project (folder name)",
    "AI_GATEWAY_URL": "the AI gateway, https://<apim>.azure-api.net",
    "AI_GATEWAY_PROJECT_URL": "the project's agent API on the gateway, <AI_GATEWAY_URL>/<gateway.path>",
    "AI_GATEWAY_AGENTS_SCOPE": "token scope for calling other agents through the gateway, <agent app client ID>/.default",
}
RESERVED_ENV = set(PLATFORM_ENV) | {"FOUNDRY_PROJECT_ENDPOINT"}
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env_file(path):
    """A dotenv file (KEY=VALUE per line; # comments, blank lines, optional quotes). Raises ValueError on a bad line."""
    values = {}
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = ENV_LINE.match(line)
            if not m:
                raise ValueError(f"line {n}: expected KEY=VALUE")
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[m.group(1)] = value
    return values


def env_files(folder):
    """A hosted agent's per-environment settings: env/<environment>.env -> {environment: {name: value}}.
    A file that can't be parsed is kept as an error for validation."""
    out = {}
    for f in sorted(glob.glob(os.path.join(folder, "env", "*.env"))):
        name = os.path.basename(f)[:-len(".env")]
        try:
            out[name] = read_env_file(f)
        except ValueError as e:
            out[name] = {"__error__": str(e)}
    return out


def hosted_environment(a, p=None, environment=None):
    """Environment variables of a hosted agent's version in one environment (the job's PLATFORM_ENVIRONMENT), lowest
    precedence first: agent.yaml `environment`, then env/<environment>.env, then the platform's (PLATFORM_ENV)."""
    environment = environment if environment is not None else os.environ.get("PLATFORM_ENVIRONMENT", "")
    values = {k: str(v) for k, v in (a.get("environment") or {}).items()}
    values.update({k: v for k, v in (a.get("env_files") or {}).get(environment, {}).items() if k != "__error__"})
    values["AZURE_AI_MODEL_DEPLOYMENT_NAME"] = a["model"]
    apim = os.environ.get("APIM_NAME", "")
    if environment:
        values["PLATFORM_ENVIRONMENT"] = environment
    if p:
        values["PLATFORM_PROJECT"] = p["folder"]
    if apim:
        values["AI_GATEWAY_URL"] = f"https://{apim}.azure-api.net"
        if p:
            values["AI_GATEWAY_PROJECT_URL"] = f"https://{apim}.azure-api.net/{(p.get('gateway') or {}).get('path', '')}"
    if os.environ.get("AGENTS_APP_ID"):   # calling other agents whose access lists gateway: token for the agent app
        values["AI_GATEWAY_AGENTS_SCOPE"] = f"{os.environ['AGENTS_APP_ID']}/.default"
    return values


def load_agent(folder, p=None):
    a = yaml.safe_load(read(folder, "agent.yaml"))
    a["folder"] = os.path.basename(folder)
    a["env_files"] = env_files(folder)
    a["kind"] = a.get("kind", "prompt")
    a["access"] = a.get("access", ["gateway"])
    # One word for each combination: gateway, foundry, or gateway+foundry (either token is accepted).
    a["access_mode"] = "+".join(x for x in ACCESS if x in (a["access"] if isinstance(a["access"], list) else []))
    a["instructions"] = read(folder, a["instructions_file"]) if a.get("instructions_file") else ""
    a["docs"] = read(folder, a["docs_file"])
    a["tests"] = yaml.safe_load(read(folder, a["tests_file"])) or []
    a["evals"] = yaml.safe_load(read(folder, a["evals_file"])) if a.get("evals_file") else None
    if a["kind"] == "hosted":
        # Two ways to ship the code: container (built from source_dir/Dockerfile into the environment's container
        # registry, the image tagged with the content hash) or code (source_dir zipped and built by Foundry).
        mode = "container" if a.get("container") else "code"
        src = (a.get(mode) or {}).get("source_dir")
        a["hosted_mode"] = mode
        a["code_zip"] = code_zip(folder, src) if src and os.path.isdir(os.path.join(folder, src)) else b""
        a["code_sha256"] = hashlib.sha256(a["code_zip"]).hexdigest()
        a["source_path"] = os.path.join(folder, src) if src else ""
        a["image_tag"] = a["code_sha256"][:16]      # same files, same tag: an unchanged agent is never rebuilt
        identity = {"kind": "hosted", "code_sha256": a["code_sha256"], mode: a.get(mode), "cpu": a.get("cpu"),
                    "memory": a.get("memory"), "protocols": a.get("protocols"),
                    "environment": hosted_environment(a, p) if a.get("model") else {}, "description": a.get("description")}
    else:
        identity = {"model": a.get("model"), "instructions": a["instructions"], "description": a.get("description")}
        if a.get("tools"):   # only when set, so agents without tools keep their existing definition hash
            identity["tools"] = a["tools"]
    a["definition_sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return a


STAGES = ("dev", "uat", "prd")


def platform_environments():
    """The stages the platform offers, in promotion order: repository variable PLATFORM_ENVIRONMENTS (comma-separated,
    e.g. "dev" or "dev,uat,prd"; default dev)."""
    return [e.strip() for e in os.environ.get("PLATFORM_ENVIRONMENTS", "dev").split(",") if e.strip()] or ["dev"]


def project_environments(p):
    """The stages a project deploys to: every stage the platform offers, unless project.yaml environments restricts
    them (it can only leave stages out, never add one)."""
    offered = platform_environments()
    return [e for e in offered if e in p["environments"]] if p.get("environments") else offered


def foundry_project(p):
    """The use case's Foundry project (foundry.project, default: the folder name), created by use-case onboarding."""
    return (p.get("foundry") or {}).get("project", p["folder"])


TOOL_USE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")   # project.yaml tools.use: <owner>/<tool>


def load_tools():
    """Every tool owned by a project of THIS repository, {"<owner project>/<tool>": definition}, from
    projects/<owner>/tools/<tool>/tool.yaml. Tools of other repositories are known only from the gateway registry."""
    tools = {}
    for f in sorted(glob.glob(os.path.join(PROJECTS, "*", "tools", "*", "tool.yaml"))):
        folder = os.path.dirname(f)
        owner = os.path.basename(os.path.dirname(os.path.dirname(folder)))
        t = yaml.safe_load(read(folder, "tool.yaml")) or {}
        t["folder"], t["owner_project"] = os.path.basename(folder), owner
        t["tests"] = (yaml.safe_load(read(folder, t["tests_file"])) or []) if t.get("tests_file") else []
        t["has_docs"] = bool(t.get("docs_file")) and os.path.isfile(os.path.join(folder, t["docs_file"]))
        tools[f"{owner}/{t['folder']}"] = t
    return tools


def tool_connection(project, key):
    """Foundry connection name of a tool (<owner>/<tool>) in a project, as use-case onboarding names it
    (onboarding/terraform/tools.tf): <project>-<tool> for its own tools, <project>-<owner>-<tool> otherwise."""
    owner, tool = key.split("/", 1)
    return f"{project}-{tool}" if owner == project else f"{project}-{owner}-{tool}"


def tool_key(project, ref):
    """An agent's tool reference as a tools.use key: "<owner>/<tool>", or "<tool>" for one of the project's own."""
    return ref if "/" in str(ref) else f"{project}/{ref}"


def project_tools(p, apim_name):
    """Tools the project's agents may use (project.yaml tools.use), keyed <owner>/<tool>: Foundry connection and
    gateway endpoint (always .../mcp), plus the definition when the owner is in this repository."""
    local = load_tools()
    return {u: {"key": u, "connection": tool_connection(p["folder"], u),
                "server_url": f"https://{apim_name}.azure-api.net/tools/{u}/mcp", "definition": local.get(u)}
            for u in ((p.get("tools") or {}).get("use") or []) if TOOL_USE_RE.match(str(u))}


def tool_registry(key):
    """The tool's registry entry on the gateway (named value tool-meta-<owner>-<tool>, written by tool-deploy)."""
    r = arm("GET", f"namedValues/tool-meta-{key.replace('/', '-')}", ok=(200, 404))
    if r.status_code == 404:
        return None
    return json.loads(r.json()["properties"]["value"])


def mcp_tools(p, a, registry=None):
    """The agent's tools (agent.yaml tools:) as Foundry MCP tool definitions. Foundry calls the gateway endpoint with
    the agent's own identity through the connection (AgenticIdentityToken); the gateway checks its tool role. Exposed tools and their impact come from the gateway registry
    (registry: {key: entry}), the same for tools of this and other repositories: allowed_tools defaults to every
    exposed tool and must be among them; require_approval defaults to always when any of them is a write tool, and
    never is refused for write tools."""
    out = []
    for x in a.get("tools") or []:
        key = tool_key(p["folder"], x["tool"])
        t = p["tools"][key]
        entry = (registry or {}).get(key) or {}
        impact = {y["name"]: y.get("impact") for y in entry.get("tools") or []}
        allowed = x.get("allowed_tools") or list(impact)
        if registry is not None:
            if not entry:
                sys.exit(f"{a['name']}: tool {key} is not published on the gateway (tool-deploy publishes it)")
            if not set(allowed) <= set(impact):
                sys.exit(f"{a['name']}: allowed_tools {sorted(set(allowed) - set(impact))} are not exposed by {key}; "
                         f"exposed: {sorted(impact)}")
            if x.get("require_approval") == "never" and any(impact.get(n) == "write" for n in allowed):
                sys.exit(f"{a['name']}: require_approval never is for read tools only; "
                         f"{sorted(n for n in allowed if impact.get(n) == 'write')} of {key} change data")
        approval = x.get("require_approval") or ("always" if any(impact.get(n) == "write" for n in allowed) else "never")
        out.append({"type": "mcp", "server_label": re.sub(r"[^A-Za-z0-9_]", "_", key.split("/", 1)[1]),
                    "server_url": t["server_url"], "project_connection_id": t["connection"],
                    "allowed_tools": allowed, "require_approval": approval})
    return out


def load_project(name):
    folder = os.path.join(PROJECTS, name)
    p = yaml.safe_load(read(folder, "project.yaml"))
    p["folder"] = name
    # Endpoint of the Foundry project in the environment's agent account (onboarding's output foundry_project_endpoint).
    # The environment comes from the GitHub environment's variables (FOUNDRY_ACCOUNT, APIM_NAME); offline validation
    # runs without them.
    account = os.environ.get("FOUNDRY_ACCOUNT", "")
    p["foundry_project_endpoint"] = f"https://{account}.services.ai.azure.com/api/projects/{foundry_project(p)}"
    p["docs"] = read(folder, p["docs_file"])
    apim = os.environ.get("APIM_NAME", "")
    p["tools_config"] = p.get("tools") or {}
    p["tools"] = project_tools(p, apim)
    p["agents"] = [load_agent(f, p) for f in sorted(glob.glob(os.path.join(folder, "agents", "*"))) if os.path.isdir(f)]
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
        errors.append(f"{n}: foundry_project_endpoint isn't a project.yaml setting; set foundry.project (onboarding creates it)")
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
        shared_envs = set(project_environments(q)) & set(project_environments(p))
        if (shared_envs
                and (q.get("models") or {}).get("connection", "platform-models") == conn):
            errors.append(f"{n}: models.connection '{conn}' is also used by {other} in environment(s) "
                          f"{', '.join(sorted(shared_envs))}; connection names are unique per Foundry account")
    return errors


def validate_hosted(scope, a):
    """Offline checks of a hosted agent: its code (container: a Dockerfile; code: entry point and runtime), size,
    protocols, no secrets."""
    errors, code = [], a.get("code") or {}
    if bool(a.get("container")) == bool(a.get("code")):
        errors.append(f"{scope}: a hosted agent needs exactly one of container (image built from a Dockerfile, the "
                      "default) or code (zip built by Foundry)")
        return errors
    if a.get("container"):
        c = a["container"]
        if not a.get("code_zip"):
            errors.append(f"{scope}: container.source_dir must be a folder with the agent's code and Dockerfile")
        elif not os.path.isfile(os.path.join(a["source_path"], c.get("dockerfile", "Dockerfile"))):
            errors.append(f"{scope}: {c.get('source_dir')}/{c.get('dockerfile', 'Dockerfile')} is missing")
        elif not os.path.isfile(os.path.join(a["source_path"], ".dockerignore")):
            errors.append(f"{scope}: {c.get('source_dir')}/.dockerignore is required (keeps .env and local files out of the image)")
    elif not a.get("code_zip"):
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
    if a.get("code") and code.get("runtime") not in HOSTED_RUNTIMES:
        errors.append(f"{scope}: code.runtime must be one of {sorted(HOSTED_RUNTIMES)}")
    if a.get("code") and code.get("dependency_resolution", "remote_build") not in ("remote_build", "bundled"):
        errors.append(f"{scope}: code.dependency_resolution must be remote_build or bundled")
    if (str(a.get("cpu")), str(a.get("memory"))) not in HOSTED_SIZES:
        errors.append(f"{scope}: cpu/memory must be one of {sorted(HOSTED_SIZES)}")
    protocols = {p.get("protocol") for p in (a.get("protocols") or [])}
    if not protocols <= HOSTED_PROTOCOLS:
        errors.append(f"{scope}: protocols must be from {sorted(HOSTED_PROTOCOLS)} (what the gateway passes through)")
    if "responses" not in protocols:
        errors.append(f"{scope}: protocols must include responses: the evaluation gate and smoke tests use it")
    sources = [("agent.yaml environment", a.get("environment") or {})] + \
              [(f"env/{e}.env", values) for e, values in (a.get("env_files") or {}).items()]
    for where, values in sources:
        if "__error__" in values:
            errors.append(f"{scope}: {where}: {values['__error__']}")
            continue
        for k in values:
            if SECRET_NAME.search(k):
                errors.append(f"{scope}: {where}: {k} looks like a secret; use Key Vault (one per environment) "
                              "with the agent's identity")
            if k in RESERVED_ENV:
                errors.append(f"{scope}: {where}: {k} is set by the platform and can't be overridden")
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)):
                errors.append(f"{scope}: {where}: {k!r} isn't a valid environment variable name")
    return errors


def validate_tools(n, p):
    """Offline checks of the tools the project owns (tools/<tool>/tool.yaml), of project.yaml tools.use and of its
    agents' tools: lists. A tool of another repository is checked against the gateway registry by use-case onboarding
    (published, shared with this project) and by deploy (allowed_tools, approvals). Whether a managed_identity tool's
    backend accepts APIM is the owning team's grant; tool-deploy's tests prove it through the gateway."""
    errors = []
    fp = foundry_project(p)
    local = load_tools()
    for key, t in local.items():
        if t["owner_project"] != n:
            continue
        ref = t["folder"]
        s = f"{n}/tools/{ref}"
        if t.get("name") != ref or not re.match(r"^[a-z0-9][a-z0-9-]{1,40}$", ref):
            errors.append(f"{s}: name must match the folder (lowercase letters, digits, hyphens; at most 41)")
        for k in ("display_name", "description", "owner"):
            if not t.get(k):
                errors.append(f"{s}: '{k}' is required")
        if t.get("type") != "mcp":
            errors.append(f"{s}: type must be mcp (an MCP server over streamable HTTP)")
        b = t.get("backend") or {}
        if not re.match(r"^https://[^/]+/.*[^/]$", str(b.get("url", ""))):
            errors.append(f"{s}: backend.url must be the https MCP endpoint, e.g. https://host/api/mcp")
        if b.get("auth", "none") not in TOOL_AUTH:
            errors.append(f"{s}: backend.auth must be one of {sorted(TOOL_AUTH)}")
        if b.get("auth") == "managed_identity" and not str(b.get("audience", "")).startswith(("api://", "https://")):
            errors.append(f"{s}: backend.auth managed_identity needs backend.audience (the backend's app ID URI)")
        exposed = [y.get("name") for y in t.get("tools") or []]
        if not exposed or len(set(exposed)) != len(exposed) or not all(exposed):
            errors.append(f"{s}: tools must list the exposed MCP tools, each once, by name")
        for y in t.get("tools") or []:
            if y.get("impact") not in TOOL_IMPACTS:
                errors.append(f"{s}: tools[{y.get('name')}].impact must be one of {sorted(TOOL_IMPACTS)}")
        rate = (t.get("gateway") or {}).get("rate_limit_per_minute")
        if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0:
            errors.append(f"{s}: gateway.rate_limit_per_minute must be a positive integer")
        for other in t.get("shared_with") or []:
            other = str(other)
            if other == fp:
                errors.append(f"{s}: shared_with lists the owner's own Foundry project {fp}; it always has access")
            elif not (NAME_RE.match(other) or re.match(r"^/subscriptions/[^/]+/resource[gG]roups/[^/]+/providers/"
                                                         r"Microsoft\.CognitiveServices/accounts/[^/]+/projects/[^/]+$", other)):
                errors.append(f"{s}: shared_with {other!r}: a Foundry project name (this environment's agent account) "
                              "or a full Foundry project resource ID")
        if not t["has_docs"]:
            errors.append(f"{s}: docs_file is required (the tool's page for agent builders)")
        tests = t.get("tests") or []
        if not any(x.get("expect") == "called" for x in tests):
            errors.append(f"{s}: tests_file needs at least one test with expect: called")
        for x in tests:
            if x.get("expect") not in TOOL_TEST_EXPECT or not x.get("input") or not x.get("tool"):
                errors.append(f"{s}: each test needs input, tool and expect ({sorted(TOOL_TEST_EXPECT)})")
            elif (x["expect"] == "called") != (x["tool"] in exposed):
                errors.append(f"{s}: test for {x['tool']}: expect called for an exposed tool, refused for any other")
    use = (p.get("tools_config") or {}).get("use") or []
    for u in use:
        if not TOOL_USE_RE.match(str(u)):
            errors.append(f"{n}: tools.use {u!r} must be <owner project>/<tool>")
        elif u in local:   # owner in this repository: check sharing now rather than at onboarding
            t = local[u]
            if t["owner_project"] != n and fp not in [str(x) for x in t.get("shared_with") or []]:
                errors.append(f"{n}: tools.use {u}: not shared with Foundry project {fp} (owner's tool.yaml shared_with)")
    if len(set(map(str, use))) != len(use):
        errors.append(f"{n}: tools.use lists a tool twice")
    for a in p["agents"]:
        for x in a.get("tools") or []:
            s = f"{n}/{a['folder']}: tools[{x.get('tool')}]"
            if a["kind"] != "prompt":
                errors.append(f"{s}: tools: is for prompt agents; a hosted agent's code calls its tools itself")
                continue
            key = tool_key(n, x.get("tool"))
            if key not in p["tools"]:
                errors.append(f"{s}: add {key} to project.yaml tools.use first; used: {sorted(p['tools']) or 'none'}")
                continue
            if x.get("require_approval", "always") not in TOOL_APPROVALS:
                errors.append(f"{s}: require_approval must be one of {sorted(TOOL_APPROVALS)}")
            t = p["tools"][key]["definition"]
            if t:   # defined in this repository: check now; otherwise deploy checks against the gateway registry
                impact = {y.get("name"): y.get("impact") for y in t.get("tools") or []}
                allowed = x.get("allowed_tools") or list(impact)
                if not set(allowed) <= set(impact):
                    errors.append(f"{s}: allowed_tools {sorted(set(allowed) - set(impact))} not exposed; use from {sorted(impact)}")
                if x.get("require_approval") == "never" and any(impact.get(k) == "write" for k in allowed):
                    errors.append(f"{s}: require_approval never is for read tools only; "
                                  f"{sorted(k for k in allowed if impact.get(k) == 'write')} change data")
    return errors


def validate(names):
    errors = []
    for n in names:
        try:
            p = load_project(n)
        except Exception as e:  # missing file, bad YAML
            errors.append(f"{n}: {e}")
            continue
        if "environment" in p:
            errors.append(f"{n}: use environments: [<env>, ...] (optional: only to restrict the stages), not environment")
        if "environments" in p:
            envs = p["environments"]
            if not isinstance(envs, list) or not envs or len(set(map(str, envs))) != len(envs) \
                    or not all(e in STAGES for e in envs):
                errors.append(f"{n}: environments (optional) restricts the stages: a list of {list(STAGES)}, each once; "
                              "leave it out to deploy to every stage the platform offers")
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
            if (not isinstance(a["access"], list) or not a["access"] or len(set(a["access"])) != len(a["access"])
                    or any(x not in ACCESS for x in a["access"])):
                errors.append(f"{n}/{an}: access must be a list of {list(ACCESS)}, each once: [gateway] (the agent app "
                              "role; default), [foundry] (the caller's Foundry role) or [gateway, foundry] (either)")
            if a["kind"] not in KINDS:
                errors.append(f"{n}/{an}: kind must be one of {sorted(KINDS)}")
            elif a["kind"] == "prompt" and len(a.get("instructions", "").strip()) < 20:
                errors.append(f"{n}/{an}: instructions are empty or too short")
            elif a["kind"] == "hosted":
                errors += validate_hosted(f"{n}/{an}", a)
            for e in a.get("env_files") or {}:
                if a["kind"] != "hosted":
                    errors.append(f"{n}/{an}: env/{e}.env: environment files are for hosted agents (a prompt agent has "
                                  "no code to read them)")
                elif e not in STAGES:
                    errors.append(f"{n}/{an}: env/{e}.env: {e} isn't a stage ({', '.join(STAGES)})")
                elif p.get("environments") and e not in p["environments"]:
                    errors.append(f"{n}/{an}: env/{e}.env: {n} doesn't deploy to {e} (project.yaml environments)")
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
        errors += validate_tools(n, p)
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
    # Exposed tools and their impact, from the gateway registry (any repository's tools), for the agents' tools: lists.
    used = {tool_key(name, x["tool"]) for a in p["agents"] for x in a.get("tools") or []}
    registry = {k: tool_registry(k) for k in sorted(used)}
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
                definition=PromptAgentDefinition(model=a["model"], instructions=a["instructions"],
                                                 tools=mcp_tools(p, a, registry) or None), metadata=metadata)
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


def agent_image(p, a):
    """The hosted agent's image in the environment's container registry (GitHub variable ACR_NAME), built from its
    source_dir and Dockerfile with ACR's cloud build (no Docker on the runner) unless that tag already exists. The tag
    is the content hash of the source, so identical code is never rebuilt and every version names its exact image."""
    import subprocess
    acr = env("ACR_NAME")
    repo = f"{p['folder']}/{a['name']}"
    image = f"{acr}.azurecr.io/{repo}:{a['image_tag']}"
    exists = subprocess.run(["az", "acr", "repository", "show", "-n", acr, "--image", f"{repo}:{a['image_tag']}", "-o", "none"],
                            capture_output=True, text=True).returncode == 0
    if exists:
        print(f"{a['name']}: image {image} already built")
        return image
    c = a["container"]
    print(f"{a['name']}: building {image} in {acr} (ACR cloud build)")
    r = subprocess.run(["az", "acr", "build", "-r", acr, "-t", f"{repo}:{a['image_tag']}",
                        "-f", os.path.join(a["source_path"], c.get("dockerfile", "Dockerfile")), a["source_path"]])
    if r.returncode:
        sys.exit(f"{a['name']}: image build failed in {acr} (see the build log above)")
    return image


def deploy_hosted(p, project, a, latest, served, metadata):
    """Create a hosted agent version without making it live (container: from its image; code: from the zip, built by
    Foundry), and wait until Foundry has it running."""
    import io
    from azure.ai.projects.models import (CodeConfiguration, ContainerConfiguration, HostedAgentDefinition,
                                          ProtocolVersionRecord)
    # Keep serving what is live: the gateway's version, else the newest version that built (never a failed one).
    active = [x for x in project.agents.list_versions(a["name"]) if (x.get("status") or "") == "active"] if latest else []
    keep_live = served or (str(max(active, key=lambda x: int(x.version)).version) if active else None)
    if keep_live:   # the upload would otherwise go live at once (selector @latest)
        pin_endpoint(p, a["name"], keep_live)
    common = dict(cpu=str(a["cpu"]), memory=str(a["memory"]), environment_variables=hosted_environment(a, p),
                  protocol_versions=[ProtocolVersionRecord(protocol=x["protocol"], version=str(x["version"]))
                                     for x in a["protocols"]])
    if a["hosted_mode"] == "container":
        image = agent_image(p, a)
        metadata = {**metadata, "image": image}
        v = project.agents.create_version(
            agent_name=a["name"], description=a["description"], metadata=metadata,
            definition=HostedAgentDefinition(**common, container_configuration=ContainerConfiguration(image=image)))
        what = f"image {image}"
    else:
        code = a["code"]
        stream = io.BytesIO(a["code_zip"])
        stream.name = f"{a['name']}.zip"
        v = project.agents.create_version_from_code(
            agent_name=a["name"], description=a["description"], metadata=metadata, code=stream,
            code_zip_sha256=a["code_sha256"],
            definition=HostedAgentDefinition(**common, code_configuration=CodeConfiguration(
                runtime=code["runtime"], entry_point=[str(x) for x in code["entry_point"]],
                dependency_resolution=code.get("dependency_resolution", "remote_build"))))
        what = f"code uploaded ({len(a['code_zip']):,} bytes)"
    pin_endpoint(p, a["name"], keep_live or v.version)   # first version: nothing else to serve
    print(f"{a['name']} v{v.version}: {what}, waiting for Foundry to start it")
    end = time.time() + 1200
    while time.time() < end:
        status = foundry_request(p, "GET", f"agents/{a['name']}/versions/{v.version}").get("status")
        if status == "active":
            break
        if status in ("failed", "deleted"):
            sys.exit(f"{a['name']} v{v.version}: {status} in Foundry; see the agent's logs in the Foundry portal "
                     "(container: the project's identity needs AcrPull on the registry)")
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
    sub = os.environ.get("APIM_SUBSCRIPTION_ID") or env("AZURE_SUBSCRIPTION_ID")
    return (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
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
    tools = ("\n\n**Tools:** " + ", ".join(f"`{x['tool']}`" for x in a["tools"])) if a.get("tools") else ""
    project, app = p["folder"], env("AGENTS_APP_ID")
    role = f"the role `agent.{project}.{a['name']}` (or `agent.{project}.all`) on the AI gateway's agent app"
    who = {
        "gateway": f"a Microsoft Entra token for the AI gateway's agent app (`Authorization: Bearer`; scope `{app}/.default`) "
                f"instead of one for Foundry. Your identity needs {role}; ask the agent's owner.",
        "foundry": "your Microsoft Entra token for Foundry (`Authorization: Bearer`; scope `https://ai.azure.com/.default`), "
                   "as for Foundry. Foundry checks your role on the project (e.g. Foundry Agent Consumer); ask the "
                   "agent's owner.",
        "gateway+foundry": f"either of your Microsoft Entra tokens (`Authorization: Bearer`): one for the AI gateway's "
                           f"agent app (scope `{app}/.default`; you need {role}), or one for Foundry (scope "
                           f"`https://ai.azure.com/.default`; Foundry checks your role on the project, e.g. Foundry "
                           f"Agent Consumer, and the agent runs as you).",
    }[a["access_mode"]]
    return (a["docs"] + f"\n\n**Call it exactly as you would call Foundry**, with this base URL in place of the agent's "
            f"Foundry address and {who} No subscription key."
            f"\n\n{usage}\n\nBody, query string, `traceparent` and session/conversation IDs pass through; responses "
            f"are Foundry's. The gateway serves the evaluated version only. **Limits per caller:** "
            f"{g['rate_limit_per_minute']} calls a minute, {g['quota_per_day']:,} a day." + tools)


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


# ----------------------------------------------------------------------------- agent roles (Entra: the gateway's agent app)
GRAPH = "https://graph.microsoft.com/v1.0"


def graph(method, path, body=None, ok=(200, 201, 204)):
    token = cred.get_token("https://graph.microsoft.com/.default").token
    r = requests.request(method, f"{GRAPH}/{path}", json=body, timeout=60,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    if r.status_code not in ok:
        hint = (" Run this as an owner of the agent app (az login as that person)." if r.status_code in (401, 403, 404) else "")
        sys.exit(f"Microsoft Graph {method} {path}: {r.status_code} {r.text[:400]}{hint}")
    return r.json() if r.text else {}


ROLE_MARKER = "[agent pipeline]"   # ends the description of every role this pipeline manages


def repository_agent_roles():
    """Every role this repository's projects need: agent.<project>.all, and agent.<project>.<agent> for each agent
    whose access lists gateway (folder names); for projects that own tools, tool.<project>.all and
    tool.<project>.<tool> (held by the agents' identities that may call the tool, and by the project's tool-smoke
    agent)."""
    roles = {}
    for proj in sorted(glob.glob(os.path.join(PROJECTS, "*", "project.yaml"))):
        project = os.path.basename(os.path.dirname(proj))
        tools = sorted(glob.glob(os.path.join(PROJECTS, project, "tools", "*", "tool.yaml")))
        if tools:
            roles[f"tool.{project}.all"] = (f"Call every tool of {project}",
                                            f"Call every tool of project {project} through the AI gateway (agent identities).")
        for tl in tools:
            tool = os.path.basename(os.path.dirname(tl))
            roles[f"tool.{project}.{tool}"] = (f"Call tool {tool} ({project})"[:100],
                                               f"Call tool {tool} of project {project} through the AI gateway (agent identities).")
        roles[f"agent.{project}.all"] = (f"Call every agent of {project}",
                                         f"Call every agent of project {project} through the AI gateway.")
        for ag in sorted(glob.glob(os.path.join(PROJECTS, project, "agents", "*", "agent.yaml"))):
            agent = os.path.basename(os.path.dirname(ag))
            if "gateway" not in ((yaml.safe_load(open(ag)) or {}).get("access") or ["gateway"]):
                continue   # called with the caller's Foundry role only: no app role
            roles[f"agent.{project}.{agent}"] = (f"Call agent {agent} ({project})"[:100],
                                                 f"Call agent {agent} of project {project} through the AI gateway.")
    return roles


def sync_agent_roles(projects):
    """`roles`: create on the gateway's agent app (AGENTS_APP_ID) every role this repository's projects need (IDs
    derived from the value), and remove the roles of the given projects' agents that no longer exist or no longer list
    gateway (Entra removes a role only once it is disabled). Roles of other repositories' projects are never touched.
    Run by an owner of the agent app (Azure CLI sign-in); who holds a role is not managed here."""
    app_id = env("AGENTS_APP_ID")
    wanted = repository_agent_roles()
    app = graph("GET", f"applications(appId='{app_id}')?$select=id,appRoles")
    roles = app.get("appRoles") or []
    have = {r["value"] for r in roles}
    add = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{app_id}/{v}")), "value": v, "isEnabled": True,
            "allowedMemberTypes": ["User", "Application"], "displayName": dn, "description": f"{d} {ROLE_MARKER}"}
           for v, (dn, d) in wanted.items() if v not in have]
    stale = [r for r in roles if any(r["value"].startswith((f"agent.{n}.", f"tool.{n}.")) for n in projects) and r["value"] not in wanted
             and (r.get("description") or "").endswith(ROLE_MARKER)]
    if any(r["isEnabled"] for r in stale):
        graph("PATCH", f"applications/{app['id']}", {"appRoles": [dict(r, isEnabled=False) if r in stale else r for r in roles]})
    if add or stale:
        graph("PATCH", f"applications/{app['id']}", {"appRoles": [r for r in roles if r not in stale] + add})
    for r in add:
        print(f"  role {r['value']}: created")
    for r in stale:
        print(f"  role {r['value']}: removed (agent or tool no longer in the project, or the agent no longer lists gateway)")
    print(f"Agent app {app_id}: {len(wanted)} role(s) for this repository's projects, {len(add)} created, {len(stale)} removed.")


def remind_agent_roles(name):
    """register: the roles callers of this project's agents need. The pipeline can't read or change Entra, so it only
    lists them; an owner of the agent app creates missing ones with `roles`."""
    needed = sorted(v for v in repository_agent_roles() if v.startswith(f"agent.{name}."))
    summary(f"**{name}**: callers need one of the roles {', '.join(f'`{v}`' for v in needed)} on the agent app "
            f"(`{env('AGENTS_APP_ID')}`). New agent? An owner of the agent app creates its role: "
            f"`python3 scripts/agent_pipeline.py roles {name}` (README: Access to agents).")
    p = load_project(name)
    uses = [(a["name"], "tool." + tool_key(name, x["tool"]).replace("/", ".")) for a in p["agents"] for x in a.get("tools") or []]
    if uses:
        summary(f"**{name}**: tool calls are authorized per agent. Each agent's own identity "
                f"(`{env('FOUNDRY_ACCOUNT')}-{foundry_project(p)}-<agent>-AgentIdentity` in Entra) needs the tool's "
                f"role, granted by the tool's owner (README: Tools): "
                + ", ".join(f"{a} → `{t}`" for a, t in uses) + ".")


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
        "protocols": ["https"], "subscriptionRequired": False, "serviceUrl": p["foundry_project_endpoint"]}})
    wait_for(f"apis/{api_id}")
    # One operation per agent and method, on /<agent>/*: the caller's Foundry path follows (fragment agent-route).
    wanted = {f"{a['name']}-{m.lower()}": (a, m) for a in p["agents"] for m in METHODS}
    for op in arm("GET", f"apis/{api_id}/operations").json().get("value", []):
        if op["name"] not in wanted:   # an operation of an agent removed from the project, or of another layout
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
                "agent-operation-policy.xml", PROJECT=name, AGENT=a["name"], KIND=a["kind"], ACCESS=",".join(a["access"]), RATE=ag["rate_limit_per_minute"],
                QUOTA=ag["quota_per_day"])}})
    print("  project and agent policies applied")

    ensure_product(g["product"])
    arm("PUT", f"products/{g['product']}/apis/{api_id}")
    print(f"  added to product {g['product']} (Developer Portal grouping, visible to the Developers group; no key needed)")

    remind_agent_roles(name)
    publish_portal(f"{name} {os.environ.get('GITHUB_SHA', 'local')[:7]}")
    base = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}"
    summary(f"**{name}**: API `{base}/<agent>/` + the agent's Foundry path · docs "
            f"https://{env('APIM_NAME')}.developer.azure-api.net/api-details#api={api_id}")


# ----------------------------------------------------------------------------- smoke test through the gateway
def transient_error(r):
    """A response that is HTTP 200 but reports a server-side failure in its body: a hosted agent's own model call
    failed upstream (error.code server_error / internal_error / rate_limit_exceeded). Retried like a 5xx."""
    if r.status_code != 200:
        return ""
    try:
        code = ((r.json() or {}).get("error") or {}).get("code") or ""
    except ValueError:
        return ""
    return code if code in ("server_error", "internal_error", "rate_limit_exceeded") else ""


def smoke(name):
    p = load_project(name)
    g = p["gateway"]
    scope = f"{env('AGENTS_APP_ID')}/.default"   # the pipeline identity holds agents.all on the agent app
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
        # The Foundry-identical path and the pipeline identity's token for the agent app (fragments agent-caller, agent-route).
        path = ("endpoint/protocols/openai/responses?api-version=v1" if a["kind"] == "hosted"
                else "openai/v1/responses")
        url = f"https://{env('APIM_NAME')}.azure-api.net/{g['path']}/{a['name']}/{path}"
        # The access list decides which token is accepted (fragment agent-caller). The pipeline identity holds agents.all on the
        # agent app and Foundry User on the agent account, so it passes every mode.
        def headers(kind):
            if kind == "none":
                return {}
            scope_ = scope if kind == "gateway" else "https://ai.azure.com/.default"
            return {"Authorization": f"Bearer {cred.get_token(scope_).token}"}
        # The tokens the agent accepts (both kinds take turns across its tests), and one that must be refused.
        kinds = [k for k in ACCESS if k in a["access"]]
        wrong, label = {"gateway": ("foundry", "Foundry token refused"), "foundry": ("gateway", "agent app token refused"),
                        "gateway+foundry": ("none", "no token refused")}[a["access_mode"]]
        wrong = headers(wrong)
        for attempt in range(6):
            r = requests.post(url, json={"input": "ping"}, headers=wrong, timeout=60)
            if r.status_code != 404:   # 404 while a new API reaches the gateway
                break
            time.sleep(15)
        ran += 1
        print(f"  {'PASS' if r.status_code == 401 else 'FAIL'}  HTTP {r.status_code}  {a['name']} ({a['access_mode']}): {label}")
        if r.status_code != 401:
            failures.append(f"{a['name']} ({a['access_mode']}): {label}: got HTTP {r.status_code}, expected 401")
        for i, t in enumerate(a["tests"]):
            kind = kinds[i % len(kinds)]
            # Retried: 401/403/404 while a new role, policy or API reaches the gateway (up to a minute); 5xx when the
            # model service is briefly unavailable upstream. A failure that lasts is still a failure after 6 attempts.
            for attempt in range(6):
                r = requests.post(url, headers=headers(kind), json={"input": t["input"]}, timeout=180)
                if r.status_code not in (401, 403, 404, 500, 502, 503, 504) and not transient_error(r):
                    break
                print(f"  retry {attempt + 1}/5  HTTP {r.status_code}{' (' + transient_error(r) + ')' if transient_error(r) else ''}  "
                      f"{a['name']}: {t['input'][:55]}")
                time.sleep(15)
            text = ""
            if r.status_code == 200:
                for item in r.json().get("output", []):
                    if item.get("type") == "message":
                        text += " ".join(c.get("text", "") for c in item.get("content", []))
            ok = r.status_code == 200 and any(w.lower() in text.lower() for w in t["must_contain_any"])
            ran += 1
            print(f"  {'PASS' if ok else 'FAIL'}  HTTP {r.status_code}  {a['name']} ({kind} token): {t['input'][:55]}")
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
    elif cmd == "roles":   # roles [project ...]: by an owner of the agent app
        sync_agent_roles(args or sorted(os.path.basename(os.path.dirname(x)) for x in glob.glob(os.path.join(PROJECTS, "*", "project.yaml"))))
    elif cmd in ("deploy", "evaluate", "register", "smoke"):
        {"deploy": deploy, "evaluate": evaluate, "register": register, "smoke": smoke}[cmd](args[0])
    else:
        sys.exit(__doc__)
