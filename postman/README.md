# Postman demo: calling agents through the AI gateway

`agent-access.postman_collection.json` calls the dev agents as a **user** with each access list (`agent.yaml`
`access`, README: [Access to agents](../README.md#access-to-agents)), shows the refusals, and has a **hosted agent
calling a prompt agent** through the gateway.

| Folder | Agent | `access` | You send | Shows |
|---|---|---|---|---|
| 1 | `models-demo/basic-code-agent` (hosted) | `[gateway]` | agent app token | an answer; a conversation that remembers (per caller); Foundry token or no token → 401 |
| 2 | `models-demo/demo-assistant` (prompt) | `[foundry]` | Foundry token | an answer; agent app token → 401; another Foundry path → 404 `AGENT_PATH_NOT_ALLOWED` |
| 3 | `models-demo/docs-assistant` (prompt + MCP tool) | `[gateway, foundry]` | both tokens (`Authorization`, `x-foundry-authorization`) | an answer from Microsoft Learn; either token alone → 401 |
| 4 | `proj-default/leave-policy-agent` (prompt) | `[gateway]` | agent app token, **without its role** | 403 `AGENT_NOT_ALLOWED` |
| 5 | `basic-code-agent` → `leave-policy-agent` | | agent app token (to basic-code-agent) | 5.1: the identity basic-code-agent calls with; 5.2: it asks leave-policy-agent with that identity (tool `ask_agent`): refused until that identity holds `agent.proj-default.leave-policy-agent`, then the answer (18 days) |

## Run it

Once agent-deploy has released both projects with access lists (the agent roles exist from then on):

1. **Access** (outside the pipelines; someone who may assign the agent app's roles and Foundry roles):
   `postman/grant_demo_access.sh`: your roles for folders 1–3 and Foundry Agent Consumer on `models-demo`.
2. **Tokens:** `postman/get_tokens.sh` writes `postman/local.postman_environment.json` (git-ignored) with your agent
   app token and your Foundry token, and prints the agent roles your token carries. Tokens last about an hour.
3. **Postman:** import `agent-access.postman_collection.json` and `local.postman_environment.json`, select the
   environment, run folders 1–4 (or the whole collection with the Runner; answers are in the Postman console).
4. **Agent to agent:** run 5.1. It prints the object ID basic-code-agent calls with (its Foundry project's identity;
   `xms_mirid` names it). Run 5.2: refused, because that identity holds no role yet. Then
   `postman/grant_demo_access.sh <object ID>`, wait a minute, and run 5.2 again: the answer comes from
   leave-policy-agent.

## How basic-code-agent calls another agent

`projects/models-demo/agents/basic-code-agent/src/main.py`, tool `ask_agent`:

```python
token = credential.get_token(os.environ["AI_GATEWAY_AGENTS_SCOPE"]).token     # its own identity, the agent app
POST {AI_GATEWAY_URL}/ai/<project>/<agent>/openai/v1/responses  (Authorization: Bearer <token>)
```

`AI_GATEWAY_URL` and `AI_GATEWAY_AGENTS_SCOPE` are set by the pipeline. The agents it may ask are listed in its
settings, `env/dev.env`: `CALLABLE_AGENTS=proj-default/leave-policy-agent`. The gateway still decides: the call gets
through only if basic-code-agent's identity holds the called agent's role (granted outside the pipelines).
