# Agent access demo (local web app)

A throwaway demo, not part of any pipeline: call the dev agents through the AI gateway with **your two tokens**,
switch which token goes in which header, and see where each call passes or stops.

```bash
python3 demo/agent-access-app/app.py        # Python 3.10+ with PyYAML; az login as the caller
# open http://127.0.0.1:8765
```

It reads `APIM_NAME` and `AGENTS_APP_ID` from the environment, else from GitHub environment `agent-dev` (gh), and the
agents (kind, `access`, sample questions) from `projects/`.

## What you see

- **Your two tokens**, decoded: the **gateway token** (for the gateway's agent app; its roles) and the **Foundry token**
  (`https://ai.azure.com`). Without any role on the agent app, Microsoft Entra issues no gateway token at all: the
  page says so.
- **Pick an agent** (its access list is shown) and **which token goes where**: `Authorization` = gateway token /
  Foundry token / none, `x-foundry-authorization` = none / Foundry token. "Use what this agent expects" sets the
  right ones.
- **Send**: the status, where the call stopped (You → Gateway → Foundry → Agent), why in plain words, the answer.
- **Compare every token choice**: the same question four ways at once (gateway token, Foundry token, both, none):
  one goes through, the others stop, side by side.

| Agent | `access` | Goes through with |
|---|---|---|
| basic-code-agent (hosted) | `[gateway]` | gateway token |
| demo-assistant | `[foundry]` | Foundry token |
| docs-assistant | `[gateway, foundry]` | both (gateway token + Foundry token in `x-foundry-authorization`) |
| leave-policy-agent, expense-policy-agent | `[gateway]` | gateway token **with their role** (else 403 `AGENT_NOT_ALLOWED`) |

Agent to agent: pick basic-code-agent and the sample "How many days of annual leave…": it asks leave-policy-agent
with **its own** gateway token (see [postman/README.md](../../postman/README.md), folder 5).

## Before it works

The access PRs released to dev, and your access granted: `postman/grant_demo_access.sh` (roles for basic-code-agent
and docs-assistant, Foundry Agent Consumer on models-demo). "Get new tokens" after a role change: roles are in new
tokens only.

Your tokens stay in the local Python process (from the Azure CLI); the page only gets what is inside them, never the
tokens. The server listens on 127.0.0.1 only.
