"""Tool tests for the tool-deploy pipeline, run after terraform apply.

  tool_smoke.py <project> <tool>          (environment: the job's variables, see platform_env.py)

For projects/<project>/tools/<tool>/: tool calls are authorized per agent, so
  1. a project's identity (the old way) is refused (401), and
  2. an identity with a token for the gateway's agent app but without the tool's role (the pipeline's own: it holds
     agents.all) is refused (403 CALLER_NOT_ALLOWED);
  3. the owner's tests (tests.yaml) pass through the gateway: the owner project's agent `tool-smoke` (kept between
     runs, a new version each run) calls the tool through a temporary RemoteTool connection with its own agent
     identity (AgenticIdentityToken). That identity must hold tool.<project>.all on the agent app, granted once by a
     person (README: Tools); the pipeline has no rights in Entra. The agent is given every tool on the server, so
     misuse tests (expect: refused) reach the gateway, which must refuse the MCP tools the owner didn't expose.
Consumers' own connections (use-case onboarding) are not involved.

Sign-in: Azure CLI. The identity needs Reader on APIM, and on the Foundry project: write connections, Foundry User.
Environment: AGENTS_APP_ID (the gateway's agent app).
"""
import os
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

from platform_env import environment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM = "https://management.azure.com"
RUN = (f"{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}" if os.environ.get("GITHUB_RUN_ID")
       else f"local-{os.getpid()}")
cred = AzureCliCredential(process_timeout=60)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    print(text)


def arm(method, path, api_version, body=None, ok=(200, 201, 202, 204)):
    token = cred.get_token(f"{ARM}/.default").token
    r = requests.request(method, f"{ARM}{path}", params={"api-version": api_version}, json=body, timeout=60,
                         headers={"Authorization": f"Bearer {token}"})
    if r.status_code not in ok:
        sys.exit(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r


def mcp_calls(resp, tool):
    """The response's MCP calls of one tool (output items of type mcp_call)."""
    return [i for i in (resp.output or []) if getattr(i, "type", "") == "mcp_call" and getattr(i, "name", "") == tool]


def main(project, tool):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    env = environment()
    p = load(os.path.join(ROOT, "projects", project, "project.yaml"))
    folder = os.path.join(ROOT, "projects", project, "tools", tool)
    t = load(os.path.join(folder, "tool.yaml"))
    tests = load(os.path.join(folder, t["tests_file"])) or []
    g, fd = env["gateway"], env["agents_foundry"]
    url = f"https://{g['apim_name']}.azure-api.net/tools/{project}/{tool}/mcp"
    fp = (p.get("foundry") or {}).get("project", project)
    project_id = (f"/subscriptions/{fd['subscription_id']}/resourceGroups/{fd['resource_group']}/providers/"
                  f"Microsoft.CognitiveServices/accounts/{fd['account_name']}/projects/{fp}")
    rows, failed = ["| Check | Result |", "|---|---|"], []

    # 1-2. Per-agent access: a project-style token, and a token for the agent app without the tool's role.
    app = os.environ["AGENTS_APP_ID"]
    for label, scope, want in (("A project's identity (token for Foundry)", "https://cognitiveservices.azure.com/.default", 401),
                               ("An identity without the tool's role (token for the agent app)", f"{app}/.default", 403)):
        token = cred.get_token(scope).token
        r = requests.post(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=60)
        rows.append(f"| {label} calling `{url}` | "
                    f"{f'refused ({want})' if r.status_code == want else f'NOT refused as expected ({r.status_code}, want {want}) (FAIL)'} |")
        if r.status_code != want:
            failed.append("access per agent")

    apim = f"/subscriptions/{g['subscription_id']}/resourceGroups/{g['resource_group']}/providers/Microsoft.ApiManagement/service/{g['apim_name']}"
    off = []
    for ks in ("ks-gateway-all", f"ks-tool-{project}-{tool}"):
        v = arm("GET", f"{apim}/namedValues/{ks}", "2024-05-01", ok=(200, 404))
        value = v.json()["properties"].get("value", "active") if v.status_code == 200 else "active"
        if str(value).lower() != "active":
            off.append(ks)
    if off:
        rows.append(f"| Owner's tests | skipped: kill switch {', '.join(off)} is not active |")
        tests = []

    # 3. The owner's tests: the project's tool-smoke agent (its own identity holds tool.<project>.all), through a
    #    temporary connection with the calling agent's identity.
    connection, agent = f"{project}-{tool}-test-{RUN}"[:60], "tool-smoke"
    identity = f"{fd['account_name']}-{fp}-{agent}-AgentIdentity"
    conn_path = f"{project_id}/connections/{connection}"
    client = AIProjectClient(endpoint=f"https://{fd['account_name']}.services.ai.azure.com/api/projects/{fp}", credential=cred)
    try:
        if tests:
            arm("PUT", conn_path, "2025-04-01-preview", {"properties": {
                "category": "RemoteTool", "group": "GenericProtocol", "target": url, "authType": "AgenticIdentityToken",
                "audience": f"api://{app}", "isSharedToAll": False, "credentials": {},
                "metadata": {"type": "custom_MCP"}}})
            time.sleep(10)
            model = f"{(p.get('models') or {}).get('connection', 'platform-models')}/{p['models']['allowed'][0]['name']}"
            client.agents.create_version(agent_name=agent, description=(
                f"Tool tests of project {project} (tool-deploy). Kept between runs: its identity holds "
                f"tool.{project}.all on the gateway's agent app."), definition=PromptAgentDefinition(
                model=model,
                instructions="You are a tool smoke test. Call exactly the tool the user names, with sensible arguments, "
                             "then report what it returned.",
                tools=[{"type": "mcp", "server_label": tool.replace("-", "_"), "server_url": url,
                        "project_connection_id": connection, "require_approval": "never"}]))
            openai = client.get_openai_client()
        for x in tests:
            expect, calls, failure = x["expect"], [], ""
            for attempt in range(2):   # the model occasionally answers without calling: ask once more
                try:
                    resp = openai.responses.create(input=x["input"], extra_body={
                        "agent_reference": {"name": agent, "type": "agent_reference"}})
                except Exception as e:   # Foundry fails the response when the tool call itself fails
                    failure = str(e)[:120]
                    break
                calls = mcp_calls(resp, x["tool"])
                if calls:
                    break
            if failure:
                rows.append(f"| {x['tool']} (expect {expect}) | FAIL: response failed: {failure}. Does `{identity}` hold "
                            f"tool.{project}.all on the agent app? (README: Tools) |")
                failed.append(x["tool"])
                continue
            errors = [str(getattr(c, "error", None) or "") for c in calls]
            # The gateway answers a tool that isn't exposed with an MCP tool error (isError) in the call's output.
            refusals = [str(getattr(c, "output", None) or "") + e for c, e in zip(calls, errors)]
            if expect == "called":
                words = x.get("must_contain_any") or []
                ok = bool(calls) and not any(errors) and not any("TOOL_NOT_ALLOWED" in r for r in refusals) and \
                    (not words or any(w.lower() in resp.output_text.lower() for w in words))
                got = "called" if calls and not any(errors) else ("error: " + errors[0][:80] if calls else "not called")
            else:
                ok = bool(calls) and all("TOOL_NOT_ALLOWED" in r for r in refusals)
                got = ("refused by the gateway" if ok else "NOT refused") if calls else "not called"
            rows.append(f"| {x['tool']} (expect {expect}) | {'pass' if ok else 'FAIL'}: {got} |")
            if not ok:
                failed.append(x["tool"])
    finally:
        if tests:   # the agent stays (its identity holds the role); only the connection is temporary
            arm("DELETE", conn_path, "2025-04-01-preview", ok=(200, 204, 404))
    summary(f"**Tool {project}/{tool}: tests through the gateway**\n\n" + "\n".join(rows))
    if failed:
        sys.exit(f"Tool tests failed: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: tool_smoke.py <project> <tool>")
    main(sys.argv[1], sys.argv[2])
