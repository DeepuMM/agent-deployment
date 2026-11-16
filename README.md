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

## Models: onboarding and access

Models are hosted in separate model-hosting Foundry accounts and reach agents only through the AI gateway.
**Model platform deployment** (`gateway/foundry-accounts/**`) deploys and registers a model; **model project connection**
(`models:` in `projects/<project>/project.yaml`) gives a project's agents the models it lists, with token limits,
through an AI Gateway connection (`model: platform-models/<model>`). Both are Terraform stacks with their own
workflows. See [gateway/README.md](gateway/README.md), including the [pipeline flow](gateway/README.md#pipeline-flow-what-happens-when-they-run). Platform coordinates per environment are in
[platform/environments/](platform/environments/README.md).

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
| `templates/project-policy.xml` | API policy: this project's limits, then the shared `project-inbound` / `project-outbound` fragments |
| `templates/agent-operation-policy.xml` | operation policy: this agent's values (kill switch, limits, name, pinned version), then the shared `agent-guardrails` / `agent-pin-version` fragments |
| `gateway/policy-fragments/<id>.xml` | the shared policy logic for agent APIs and project model APIs, one APIM policy fragment per file, deployed by `gateway-policy-deployment` (see below) |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `evaluate`, `register`, `smoke` |
| `.github/workflows/agent-ci.yml` | pull requests: validate |
| `.github/workflows/agent-deploy.yml` | main: every changed project (one at a time); or run on demand for one project |
| `gateway/` | model platform deployment and model project connection (Terraform), see [gateway/README.md](gateway/README.md) |
| `platform/environments/<env>.yaml` | platform coordinates per environment |
| `.github/workflows/model-platform-deployment.yml`, `model-project-connection.yml` | the two model pipelines |
| `.github/workflows/gateway-policy-deployment.yml` | the shared gateway policy fragments, every environment (stack `gateway/terraform/gateway-policies`) |

## Shared policy (policy fragments)

The gateway logic every agent shares (kill switch check, rate limit and quota, forwarding to Foundry with managed
identity, pinning the evaluated version) is in APIM **policy fragments**, one per file in `gateway/policy-fragments/`.
Each agent operation and project API policy only sets its own values as context variables and includes the fragments:

```xml
<set-variable name="agent" value="expense-policy-agent" />
<set-variable name="agent-status" value="{{ks-agent-expense-policy-agent}}" />
<set-variable name="agent-rate" value="@(30)" />  ...
<include-fragment fragment-id="agent-guardrails" />
<quota-by-key calls="2000" renewal-period="86400" counter-key="..." />
<base />
<include-fragment fragment-id="agent-pin-version" />
```

| Change | Pipeline | Agents redeployed |
|---|---|---|
| `gateway/policy-fragments/*.xml` (shared logic) | `gateway-policy-deployment`: plan on the PR, apply on merge (environment approval), for **every** environment; then smoke-tests every project model API and agent on that gateway | none: every agent and model API uses the new fragment at once |
| `templates/*.xml` (which values are passed, which fragments are included) | `agent-deploy`, every project | all |
| an agent's limits or version | `agent-deploy`, that project | that project |

| Fragment | Included by | Does |
|---|---|---|
| `agent-guardrails` | agent operation policy, before `<base />` | kill switch (503), agent rate limit |
| `agent-pin-version` | agent operation policy, after `<base />` | fixes `agent_reference` to the evaluated version |
| `project-inbound` | project API policy | project rate limit, correlation ID, Responses API, managed identity |
| `project-outbound` | project API policy, project model API policy | correlation ID on the response |
| `model-api-caller` | project model API policy (`gateway/terraform/project-connection`) | only the project's Foundry identity (token claim `xms_mirid`; tenant from named value `gateway-tenant-id`) |
| `model-api-route` | project model API policy, after its routing table | 404 `MODEL_NOT_ALLOWED`, model kill switch, token limit, backend, managed identity |

The project model API policy keeps only its routing table (allowed models with their kill switch, token limits,
backend and deployment).

The daily quotas (`quota-by-key`) stay in the agent operation and project policies, with literal values: APIM accepts
no policy expression in `quota-by-key calls`. The rate limits and token limits take their values from context
variables (`llm-token-limit token-quota` must be a `long`, hence `@(…L)`).

### How the fragments are deployed

Only the `gateway-policy-deployment` workflow writes fragments (Terraform stack `gateway/terraform/gateway-policies`,
state `ai-gateway-policies-<env>.tfstate`). It runs for **every** environment in `platform/environments/`, in that
environment's model GitHub environment (`test` → `model-test`), filtered by `MODEL_ENVIRONMENTS` when set:

1. Pull request: format, validate, plan tests, and a plan per environment. The plan fails if a policy template
   (`templates/*.xml`, `gateway/terraform/project-connection/policies/*.tftpl`) includes a fragment with no file.
2. Merge to `main`: plan → approval → apply → smoke tests of every project model API and agent on that gateway.

The other pipelines only reference fragments by name. `agent-deploy` (`register`) and the `model-project-connection`
plan check that the fragments they include are on the gateway, and stop with "run gateway-policy-deployment first"
if not.

### Change the shared policy

1. Edit `gateway/policy-fragments/<id>.xml` (the file name is the fragment ID; the root element is `<fragment>`).
   A fragment reads its inputs from context variables; the comment at the top of each file lists them.
2. Open a pull request and review the plan of every environment.
3. Merge and approve the apply per environment. Every agent and model API uses the new logic at once.

A fragment change has no evaluation gate and reaches every project at once: review it like platform code. A new
fragment must be deployed (merged and applied) before a template that includes it is merged.

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
