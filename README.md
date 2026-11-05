# Agent deployment

Deploys Microsoft Foundry prompt agents from Git, gates each new version with a Foundry evaluation, and
publishes them on the AI gateway (Azure API Management) with developer documentation in the APIM Developer
Portal.

**One Foundry project = one API. Each agent in the project = one operation of that API.**

```
POST https://<apim>.azure-api.net/ai/<project>/<agent>/responses
```

Merge a change under `projects/<project>/` → GitHub Actions, per changed project:

1. **Validate** the project and agent definitions.
2. **Deploy to Foundry** — a new agent version only for agents whose model, instructions or description changed.
   The gateway keeps serving the previous version.
3. **Evaluate (gate)** — a Foundry evaluation of each new version against its `evals.yaml` dataset with the
   built-in evaluators (intent resolution, task adherence, relevance, coherence). Every evaluator must reach
   `min_pass_rate`; otherwise the run stops and the gateway stays on the previous version. Runs appear on the
   agent's **Evaluations** tab in Foundry.
4. **Register in the AI gateway** — named values per agent (`agent-name-<agent>`, **`agent-version-<agent>`**
   pinned to the evaluated version, kill switch `ks-agent-<agent>`), the project API imported from a generated
   OpenAPI spec (one operation per agent), the project and agent policies, the product.
5. **Publish developer docs** — `project.md` becomes the API description and each agent's `docs.md` its
   operation description; the Developer Portal is republished.
6. **Smoke test** — every agent's `tests.yaml` through the gateway.

## Layout

| Path | What it is |
|---|---|
| `projects/<project>/project.yaml` | Foundry project endpoint, gateway path, product, project rate limit and quota |
| `projects/<project>/project.md` | API documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/agent.yaml` | name, description, owner, model, agent rate limit and quota |
| `projects/<project>/agents/<agent>/instructions.md` | the agent's instructions |
| `projects/<project>/agents/<agent>/docs.md` | operation documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/evals.yaml` | evaluation gate: judge model, minimum pass rate, evaluators, dataset |
| `projects/<project>/agents/<agent>/tests.yaml` | smoke tests (`input`, `must_contain_any`) |
| `templates/project-policy.xml` | API policy: project limits, correlation ID, forward to Foundry, managed identity |
| `templates/agent-operation-policy.xml` | operation policy: kill switch, agent limits, agent name and pinned version |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `evaluate`, `register`, `smoke` |
| `.github/workflows/agent-ci.yml` | pull requests: validate |
| `.github/workflows/agent-deploy.yml` | main: every changed project (one at a time); or run on demand for one project |

## Add an agent

1. Copy `projects/proj-default/agents/leave-policy-agent` to `projects/<project>/agents/<new-agent>`
   (lowercase, digits, hyphens).
2. Edit `agent.yaml` (name must match the folder), the instructions, docs, evaluation dataset and smoke tests.
3. Open a pull request; `agent-ci` validates it. Merge to deploy.

## Add a project

Create `projects/<project>/project.yaml` (endpoint of the Foundry project, `gateway.path` = `ai/<project>`)
and `project.md`, then add agents. A project's consumers subscribe to its product; one key covers every agent
in the project. Put an agent in its own project when it needs a different audience.

## Kill switch and version

- **Stop an agent:** APIM → Named values → `ks-agent-<agent>` → `suspended`. Callers get `503 AI_ROUTE_SUSPENDED`.
  The pipeline creates the kill switch once and never resets it; the smoke test skips suspended agents.
- **Version served:** `agent-version-<agent>`. Only the pipeline moves it, and only to a version that passed
  evaluation in the same run. To roll back, set it to an earlier version (or revert the change in Git and merge).

## Environment (`agent-test`)

| Variable | Value |
|---|---|
| `APIM_NAME` / `APIM_RESOURCE_GROUP` | `apim-agents-3471ad` / `az-rg-tst-model-npr-01` |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | managed identity `id-promptops-pipeline`, signed in with GitHub OIDC (no secrets) |

The Foundry project endpoint is in each `project.yaml`. The identity needs **Foundry User** on the Foundry
account (agents and evaluations) and **API Management Service Contributor** on the APIM instance. Its federated
credential `github-agent-deployment` trusts
`repo:DeepuMM@3176338/agent-deployment@1445709242:environment:agent-test`; update it if the repo moves.
APIM's own managed identity needs **Foundry User** on the Foundry account so the gateway can call the agents.

## One-time setup per APIM instance

- **Developer Portal:** open it once from the Azure portal (APIM → Developer portal) so Azure provisions it;
  after that every deployment publishes it.
- **Sign-in:** the portal uses Microsoft Entra ID (app registration `apim-agents-3471ad developer portal`);
  self sign-up with email and password is off. Renew the app secret before it expires (November 2027).

## Run locally

```bash
pip install -r scripts/requirements.txt
az login
export AZURE_SUBSCRIPTION_ID=... APIM_RESOURCE_GROUP=... APIM_NAME=...
python scripts/agent_pipeline.py validate
python scripts/agent_pipeline.py deploy   proj-default
python scripts/agent_pipeline.py evaluate proj-default
python scripts/agent_pipeline.py register proj-default
python scripts/agent_pipeline.py smoke    proj-default
```
