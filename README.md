# Agent deployment

Deploys Microsoft Foundry prompt agents from Git and publishes each one on the AI gateway (Azure API
Management) with its developer documentation in the APIM Developer Portal.

Merge a change to an agent folder → GitHub Actions:

1. **Validate** the agent definition.
2. **Deploy to Foundry** — creates a new agent version only when the model, instructions or description changed.
3. **Register in the AI gateway** — named values (`agent-name-<agent>`, kill switch `ks-agent-<agent>`),
   API imported from a generated OpenAPI spec, gateway policy, product.
4. **Publish developer docs** — the agent's `docs.md` becomes the API description in the Developer Portal,
   with request/response examples and error codes.
5. **Smoke test** — runs `tests.yaml` through the gateway with a subscription key.

## Layout

| Path | What it is |
|---|---|
| `agents/<agent>/agent.yaml` | name, description, owner, model, gateway settings (path, product, rate limit, quota) |
| `agents/<agent>/instructions.md` | the agent's instructions |
| `agents/<agent>/docs.md` | developer documentation shown in the APIM Developer Portal |
| `agents/<agent>/tests.yaml` | smoke tests (`input`, `must_contain_any`) |
| `templates/agent-policy.xml` | gateway policy: kill switch, throttling, forward to the Foundry agent, managed identity |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `register`, `smoke` |
| `.github/workflows/agent-ci.yml` | pull requests: validate |
| `.github/workflows/agent-deploy.yml` | main: deploy every changed agent (one at a time); or run on demand for one agent |

## Add an agent

1. Copy `agents/leave-policy-agent` to `agents/<new-agent>` (lowercase, digits, hyphens).
2. Edit `agent.yaml` (name must match the folder; `gateway.path` = `agents/<new-agent>`), the instructions,
   the docs and the tests.
3. Open a pull request; `agent-ci` validates it. Merge to deploy.

## Kill switch

The pipeline creates `ks-agent-<agent>` = `active` on the first deployment only, so a deployment never
re-enables a suspended agent. To stop an agent: APIM → Named values → `ks-agent-<agent>` → `suspended`.
Callers then get `503 AI_ROUTE_SUSPENDED`. The smoke test is skipped while an agent is suspended.

## Environment (`agent-test`)

| Variable | Value |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | `https://az-aif-tst-model-npr-01.services.ai.azure.com/api/projects/proj-default` |
| `APIM_NAME` / `APIM_RESOURCE_GROUP` | `apim-agents-3471ad` / `az-rg-tst-model-npr-01` |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | managed identity `id-promptops-pipeline`, signed in with GitHub OIDC (no secrets) |

The identity needs **Foundry User** on the Foundry account and **API Management Service Contributor** on
the APIM instance. Its federated credential `github-agent-deployment` trusts
`repo:DeepuMM@3176338/agent-deployment@1445709242:environment:agent-test`; update it if the repo moves.

APIM's own managed identity needs **Foundry User** on the Foundry account (already granted) so the gateway
can call the agent.

## One-time setup: Developer Portal

Azure requires the Developer Portal to be opened once in admin mode before it can be published: Azure portal
→ APIM `apim-agents-3471ad` → **Developer portal**. After that, every deployment publishes the portal so new
or changed agent docs appear at `https://apim-agents-3471ad.developer.azure-api.net`.

## Run locally

```bash
pip install -r scripts/requirements.txt
az login
export FOUNDRY_PROJECT_ENDPOINT=... AZURE_SUBSCRIPTION_ID=... APIM_RESOURCE_GROUP=... APIM_NAME=...
python scripts/agent_pipeline.py validate
python scripts/agent_pipeline.py deploy leave-policy-agent
python scripts/agent_pipeline.py register leave-policy-agent
python scripts/agent_pipeline.py smoke leave-policy-agent
```
