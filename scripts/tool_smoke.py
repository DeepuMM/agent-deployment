"""Tool tests for the tool-deploy pipeline, run after terraform apply.

  tool_smoke.py <project> <tool> [environment]

For projects/<project>/tools/<tool>/: nobody outside the tool's auth guard can call it (the pipeline's own token is
refused), and the owner's tests (tests.yaml) pass through the gateway: a temporary RemoteTool connection and a
temporary prompt agent in the owner's Foundry project call the tool with the project's identity, then both are
deleted. The agent is given every tool on the server, so misuse tests (expect: refused) reach the gateway, which must
refuse the MCP tools the owner didn't expose. Consumers' own connections (use-case onboarding) are not involved.

Sign-in: Azure CLI. The identity needs Reader on APIM, and on the Foundry project: write connections, Foundry User.
"""
import os
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

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


def main(project, tool, env_name):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    env = load(os.path.join(ROOT, "platform", "environments", f"{env_name}.yaml"))
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

    # 1. The auth guard: the pipeline's own token is not a Foundry project in shared_with.
    token = cred.get_token("https://cognitiveservices.azure.com/.default").token
    r = requests.post(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=60)
    rows.append(f"| Another identity calling `{url}` | "
                f"{'refused (401)' if r.status_code == 401 else f'NOT refused ({r.status_code}) (FAIL)'} |")
    if r.status_code != 401:
        failed.append("auth guard")

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

    # 2. The owner's tests, through a temporary connection and agent in the owner's Foundry project.
    connection, agent = f"{project}-{tool}-test-{RUN}"[:60], f"tool-smoke-{RUN}"
    conn_path = f"{project_id}/connections/{connection}"
    client = AIProjectClient(endpoint=f"https://{fd['account_name']}.services.ai.azure.com/api/projects/{fp}", credential=cred)
    try:
        if tests:
            arm("PUT", conn_path, "2025-04-01-preview", {"properties": {
                "category": "RemoteTool", "group": "GenericProtocol", "target": url, "authType": "ProjectManagedIdentity",
                "audience": "https://cognitiveservices.azure.com", "isSharedToAll": False, "credentials": {},
                "metadata": {"type": "custom_MCP"}}})
            time.sleep(10)
            model = f"{(p.get('models') or {}).get('connection', 'platform-models')}/{p['models']['allowed'][0]['name']}"
            client.agents.create_version(agent_name=agent, definition=PromptAgentDefinition(
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
                rows.append(f"| {x['tool']} (expect {expect}) | FAIL: response failed: {failure} |")
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
        if tests:
            try:
                client.agents.delete(agent_name=agent)
            except Exception as e:  # nothing to delete when the create failed
                print(f"  (cleanup agent: {e})")
            arm("DELETE", conn_path, "2025-04-01-preview", ok=(200, 204, 404))
    summary(f"**Tool {project}/{tool}: tests through the gateway**\n\n" + "\n".join(rows))
    if failed:
        sys.exit(f"Tool tests failed: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: tool_smoke.py <project> <tool> [environment]")
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "test")
