# Agent deployment (project repository)

Onboards use cases onto the AI platform and deploys their agents. Each project gets **its own Microsoft Foundry
project** and governed access to platform models (use-case onboarding); then its agents are deployed, gated by a
Foundry evaluation, and published on the AI gateway (Azure API Management) with documentation in the Developer Portal.

The platform itself lives in the gateway repository, **[ai-gateway](https://github.com/DeepuMM/ai-gateway)**: the
gateway, model hosting, and the shared gateway policy that every project uses (kill switches, content safety,
routing, limits). See its README for the APIM structure, the policy fragments and how the kill switches work.

## Pipelines

Three GitHub Actions pipelines, all signed in to Azure with GitHub OIDC (no secrets). A merge runs them in this order:

```
projects/<project>/project.yaml changed ──► use-case-onboarding ──► agent-deploy
projects/<project>/agents/** changed   ─────────────────────────► agent-deploy
pull request                          ──► agent-ci (+ use-case-onboarding plan when project.yaml or onboarding/ changed)
```

### 1. `use-case-onboarding`: create the use case and connect its models

| | |
|---|---|
| **What it does** | Creates the use case's **Foundry project** in the environment's agent Foundry account (or adopts one: `foundry.existing: true`), then on the gateway its **model API** `models/<project>` (only this project's identity, routing per allowed model, token limits, content safety settings), its **token allocation** `model-limit-<project>-<model>` (checked against free capacity), and in the Foundry project the **AI Gateway connection** (`models.connection`) its agents use as `<connection>/<model>`. Then a smoke test: a temporary agent in the project answers through each model, and another identity calling the model API is refused (`401`). Publishes the Developer Portal. |
| **Runs** | called by `agent-deploy` for every project whose `project.yaml` changed (before its agents); pull requests: plan only; a change under `onboarding/`: every project; on demand for one project |
| **Reads** | `projects/<project>/project.yaml`; `platform/environments/<env>.yaml` (gateway, agent Foundry account, tenant, Terraform state); the model registry and policy fragments **from APIM** |
| **When the run fails** | **In the plan, so nothing has changed yet:** a model isn't registered on the gateway or has no free capacity left, a shared fragment is missing on the gateway, another project already uses the same Foundry project or connection name, or content safety is set looser than the platform. **During the apply:** Azure refuses a change (for example a permission is missing). |
| **Terraform** | `onboarding/terraform`, state `ai-gateway-model-access-<project>-<env>.tfstate` |
| **Shows in the run summary** | the plan, the **Foundry project URL** and the **model API URL** |

### 2. `agent-deploy`: release the agents

| | |
|---|---|
| **What it does** | 1. **Validate** the project and agent files. 2. **Deploy** a new Foundry agent version, only for agents whose model, instructions or description changed (not live yet). 3. **Evaluation gate**: a Foundry evaluation of each new version against its `evals.yaml`; every evaluator must reach `min_pass_rate`, otherwise the run stops and the gateway keeps the previous version. 4. **Register** on the gateway: agent API `ai/<project>` (one operation per agent), each agent pinned to its evaluated version (`agent-version-<agent>`), its kill switch `ks-agent-<agent>` (created once, never reset), the product. 5. **Publish** `project.md` and each `docs.md` to the Developer Portal. 6. **Smoke test** every agent's `tests.yaml` through the gateway. |
| **Runs** | merge to `main` touching `projects/**` or `templates/**` (every changed project, one at a time); on demand for one project |
| **Reads** | `projects/<project>/**`, `templates/*.xml`, `platform/environments/<env>.yaml` (agent Foundry account) |
| **Shows in the run summary** | each agent version and its evaluation result (with a link to the Foundry report), the **agent API URL** and the Developer Portal link, the smoke test results |

### 3. `agent-ci`: check a pull request

Validates every project and agent file (names, required files, models and judges, limits, content safety, Foundry
project and connection names). No Azure access.

### Variables

Set per GitHub environment, as **variables**, not secrets. Test has two: `model-test` (onboarding) and `agent-test`
(agent-deploy); any other platform environment has one of its own name (e.g. `sandbox`).

| Variable | Used by | Value (test) |
|---|---|---|
| `AZURE_CLIENT_ID` | onboarding, agent-deploy | client ID of the pipeline identity `id-promptops-pipeline` |
| `AZURE_TENANT_ID` | onboarding, agent-deploy | the platform tenant |
| `AZURE_SUBSCRIPTION_ID` | onboarding, agent-deploy | the subscription of APIM and the agent Foundry account |
| `APIM_NAME` | agent-deploy | `apim-agents-3471ad`: must match `gateway.apim_name` in the environment file |
| `APIM_RESOURCE_GROUP` | agent-deploy | `az-rg-tst-model-npr-01`: must match `gateway.resource_group` |
| `PORTAL_ANONYMOUS` | agent-deploy (optional) | `true`: products visible in the portal without sign-in |

Repository variable: `MODEL_ENVIRONMENTS`, the environments onboarding may run in (`test`).

Not variables, because they are worked out from the files, so they can't drift apart:

| Value | Comes from | Example (test) |
|---|---|---|
| **Gateway URL** | `https://<gateway.apim_name>.azure-api.net` | `https://apim-agents-3471ad.azure-api.net` |
| **Foundry project URL** (where agents are deployed) | `https://<agents_foundry.account_name>.services.ai.azure.com/api/projects/<foundry.project>`; also onboarding's output `foundry_project_endpoint` | `https://az-aif-tst-model-npr-01.services.ai.azure.com/api/projects/proj-default` |
| Agent base URL | `<gateway URL>/ai/<project>/<agent>/` + the agent's Foundry path ([Call an agent](#call-an-agent-through-the-gateway)) | `…/ai/proj-default/leave-policy-agent/openai/v1/responses` |
| Model API URL (used only by the project's own agents) | `<gateway URL>/models/<project>` | `…/models/proj-default` |
| Terraform state storage | `terraform_state` in the environment file | `stscbxtf3471ad5a` / `tfstate` |

### Permissions

The pipeline identity (one per environment) needs:

| Role | Scope | For |
|---|---|---|
| **Foundry Project Manager** (or Contributor) | the agent Foundry account | create the Foundry project and its AI Gateway connection (onboarding) |
| **Foundry User** | the agent Foundry account | agent versions, evaluation runs, the smoke-test agent (onboarding, agent-deploy) |
| **API Management Service Contributor** | the APIM instance | model API, agent API, named values, products, portal, smoke-test subscription |
| **Storage Blob Data Contributor** | the Terraform state container | onboarding state |
| Federated credential | the identity, trusting `repo:<owner>@<id>/agent-deployment@<id>:environment:<env>` | GitHub OIDC sign-in |

Set up by the gateway repository, not here: APIM's managed identity has **Foundry User** on the agent account (so the
gateway can call the agents); the Foundry projects' own identities need no role (they only call the gateway). In the
sandbox, `id-sbx-pipeline` has Contributor on the resource group, which covers the Contributor-level rows.

## Two repositories

| | **ai-gateway** (platform team) | **agent-deployment**, this repository (use-case teams) |
|---|---|---|
| Platform | APIM, Foundry accounts for models and for agents, Content Safety (`gateway/sandbox`); environment files (source of truth) | a copy of the environment files it deploys to |
| Model hosting | `model-platform-deployment`: models, registry, catalog | — |
| Shared gateway policy | `gateway-policy-deployment`: policy fragments, global kill switch, Content Safety backend | — |
| **Use-case onboarding** | — | `use-case-onboarding`: the use case's **Foundry project**, its model API, token budget, content safety settings and **connected models** |
| **Agent deployment** | — | `agent-deploy`: agent versions, evaluation gate, gateway publishing, docs, smoke test |

This repository reads what the gateway serves (the model registry, the shared fragments) from APIM, never from the
gateway repository's files, and stops with a clear message if the gateway isn't ready.

## Layout

| Path | What it is |
|---|---|
| `projects/<project>/project.yaml` | Foundry project, environment, gateway path and product, project limits, models and content safety |
| `projects/<project>/project.md` | API documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/agent.yaml` | name, description, owner, model (`<connection>/<model>`), agent limits |
| `projects/<project>/agents/<agent>/instructions.md` | the agent's instructions |
| `projects/<project>/agents/<agent>/docs.md` | operation documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/evals.yaml` | evaluation gate: judge model, minimum pass rate, evaluators, dataset |
| `projects/<project>/agents/<agent>/tests.yaml` | smoke tests (`input`, `must_contain_any`) |
| `onboarding/terraform` | use-case onboarding stack (state `ai-gateway-model-access-<project>-<env>.tfstate`) |
| `templates/` | the agent operation and project API policies: each agent's and project's own values, plus the shared fragments by name |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `evaluate`, `register`, `smoke` |
| `scripts/model_smoke.py` | onboarding smoke test (`access`) |
| `platform/environments/<env>.yaml` | copy of the platform coordinates (the gateway repository owns them) |
| `.github/workflows/` | `agent-ci` (pull requests), `use-case-onboarding`, `agent-deploy` |

## Use-case onboarding

One Terraform run per project creates everything the use case needs before its first agent:

| Created | Where | Notes |
|---|---|---|
| **Foundry project** `foundry.project` | the environment's agent Foundry account | with its own managed identity; the account has no model deployments |
| Model API `models-<project>`, path `models/<project>` | APIM | only this project's Foundry identity may call it (`401` otherwise); routing table per allowed model with its token limits and content safety settings; includes the shared fragments |
| Token allocation `model-limit-<project>-<model>` | APIM named value | checked against the model's capacity minus other projects' shares |
| **AI Gateway connection** `models.connection` | the Foundry project | agents use `"<connection>/<model>"`; static list of the allowed models |
| Product link, API reference | APIM, Developer Portal | |

```yaml
# projects/<project>/project.yaml
name: proj-default
environment: test
display_name: Default project (test)
owner: ai-platform-team
foundry:
  project: proj-default          # Foundry project to create (default: the folder name)
  # existing: true               # adopt a project made before onboarding, instead of creating it
gateway:
  path: ai/proj-default
  product: agents-test
  rate_limit_per_minute: 120
  quota_per_day: 10000
models:
  connection: proj-default-models  # unique per agent Foundry account
  allowed:
    - name: gpt-5.4-mini           # a model registered on the gateway
      tokens_per_minute: 20000
      tokens_per_day: 500000
```

Agents then set `model: <connection>/<model>` (and the evaluation judge `judge_model` the same way).

**Rules** (checked by `agent-ci` on the pull request and by the onboarding plan, before anything changes):

- `models.allowed` is required: agents reach models only through the gateway.
- Every model is registered on the gateway, and the requested tokens per minute fit in its free capacity.
- **One Foundry project per repo project:** no two projects name the same `foundry.project`.
- **Connection names are unique per agent Foundry account:** Foundry refuses a second connection with the same name
  in another project of the same account. In the sandbox, `models-demo` has `platform-models` and `proj-default` has
  `proj-default-models`.
- Content safety settings can only be stricter than the platform (below).
- The shared policy fragments must be on the gateway (deployed by the gateway repository).

### Content safety per project and per model

Every prompt to a platform model is checked by Azure AI Content Safety at the gateway (shared fragment
`model-content-safety`, in the gateway repository). Settings are resolved **per model** of each project:

1. the **model's** own setting: `models.allowed[].content_safety`
2. else the **project's** setting: `models.content_safety` (every model of the project)
3. else the **platform default**: severity `4` per category, Prompt Shields on, answer checks **off**

Settings can only be **stricter** than the platform: a category (`hate`, `sexual`, `self_harm`, `violence`) from `0`
to `4` (lower blocks more), and `prompt_shields` can only be `true`.

**Answer checks are opt-in** (`check_answers: true`, default `false`): the model's answers are then checked too, with
the same categories and thresholds as the prompts. Off by default because each checked answer costs one Content
Safety call per 1,000 characters and adds latency, and a Content Safety outage would then also cut off legitimate
answers. Foundry streams its model calls, so a flagged answer isn't replaced by a `403`: the stream stops at the first
flagged 1,000-character window and the agent's caller gets an error ("an error occurred while streaming the
response"). Without it, answers are still filtered by the model deployment's own guardrail. Example: with
`violence: 2` and `check_answers: true`, "Summarise the plot of Macbeth, including how each major character dies."
(prompt Violence 1, answer Violence 3) is stopped; with the defaults it is answered.

```yaml
models:
  content_safety:              # project level, optional
    self_harm: 3
  allowed:
    - name: gpt-5.4-mini       # self_harm 3 (project), others 4 (platform)
      tokens_per_minute: 20000
      tokens_per_day: 500000
    - name: other-model
      tokens_per_minute: 5000
      tokens_per_day: 50000
      content_safety:          # model level, optional: this model only
        violence: 2            # violence 2, self_harm 3, others 4
        check_answers: true    # opt-in: this model's answers are checked too (default false)
```

`proj-default` sets `violence: 2`; `models-demo` uses the platform defaults. Neither checks answers. A known gateway issue makes Content
Safety outages look like content blocks (`403`); see the gateway repository's README.

**Proof on every onboarding run.** The onboarding smoke test sends one fixed prompt, "A soldier was wounded by gunfire
during the battle." (Violence severity 3, every other category 0), through a temporary agent in the project. A
category is blocked at severity >= its threshold, so the same prompt must be **blocked** in `proj-default` (threshold
2) and **answered** in `models-demo` (platform default 4); either result being wrong fails the run. The step summary
shows the expected and actual result per project.

## Projects in the test environment

| Project | Foundry project | Connection | Agent API | Tokens per minute | Content safety |
|---|---|---|---|---|---|
| `models-demo` | `models-demo` (created by onboarding) | `platform-models` | `ai/models-demo` | 20,000 of `gpt-5.4-mini`'s 200,000 | platform defaults |
| `proj-default` | `proj-default` (adopted: existed before onboarding) | `proj-default-models` | `ai/proj-default` | 20,000 | `violence: 2` |

Agents: `models-demo`: `demo-assistant` (prompt), `basic-code-agent` (hosted, code); `proj-default`:
`expense-policy-agent`, `leave-policy-agent` (prompt).

## Agent kinds

| | Prompt agent (`kind: prompt`, default) | Hosted (code) agent (`kind: hosted`) |
|---|---|---|
| What it is | a model + instructions, run by Foundry | your Python code (e.g. Microsoft Agent Framework), built and run by Foundry |
| Folder | `agent.yaml`, `instructions.md`, `docs.md`, `tests.yaml`, `evals.yaml` | the same (instructions optional) + `src/` with the code and `pyproject.toml` or `requirements.txt` |
| Example | `proj-default/agents/leave-policy-agent` | `models-demo/agents/basic-code-agent` (Foundry sample `hosted-agents/agent-framework/responses/01-basic`) |
| Model | `model: <connection>/<model>` | the same; the pipeline gives it to the code as `AZURE_AI_MODEL_DEPLOYMENT_NAME`, so model calls go through the gateway like every agent's |
| Deploy | new version when instructions/model change | the pipeline zips `code.source_dir` (deterministic: the hash only changes with the code), uploads it as a new version, Foundry installs the dependencies (`remote_build`) and builds it; container images (ACR) come later |
| Version served | `agent_reference` set by the gateway | the agent endpoint's version selector, 100% to the evaluated version. Before each upload the pipeline pins it to the version that is live (never a failed build): Foundry's default `@latest` would serve new code before evaluation |
| Protocols | Responses | `responses` (required: evaluation and smoke tests use it), optionally `invocations` |

A hosted agent's `agent.yaml` (see `projects/models-demo/agents/basic-code-agent/agent.yaml`):

```yaml
kind: hosted
model: platform-models/gpt-5.4-mini       # reaches the code as AZURE_AI_MODEL_DEPLOYMENT_NAME
code:
  source_dir: src
  runtime: python_3_13                     # python_3_11 | python_3_12 | python_3_13
  entry_point: [python, main.py]
  dependency_resolution: remote_build
cpu: "0.5"                                 # with memory: 0.25/0.5Gi, 0.5/1Gi, 1/2Gi, 2/4Gi
memory: 1Gi
protocols:
  - {protocol: responses, version: 2.0.0}
environment: {PYTHONNOUSERSITE: "1"}      # no secrets (checked): use Key Vault with the agent's identity
```

`agent-ci` checks hosted agents too: runtime, size, protocols, entry point, dependency manifest, no secret-looking
environment variables, and that the code compiles.

## Call an agent through the gateway

The gateway is **transparent**: send exactly the request you would send to Foundry, with
`<gateway URL>/ai/<project>/<agent>/` in place of the agent's Foundry address, **your own Microsoft Entra token for
`https://ai.azure.com`** (`Authorization: Bearer`, as for Foundry) and your **subscription key**
(`Ocp-Apim-Subscription-Key`). Body, query string (`api-version`, `agent_session_id`), `traceparent`/`tracestate` and
`x-client-*` headers pass through; Foundry's response comes back unchanged (`x-agent-session-id`, `x-request-id`, …).

| Kind | Foundry, direct | Through the gateway |
|---|---|---|
| hosted | `{project URL}/agents/<agent>/endpoint/protocols/openai/responses?api-version=v1` | `{gateway}/ai/<project>/<agent>/endpoint/protocols/openai/responses?api-version=v1` |
| hosted | `…/agents/<agent>/endpoint/protocols/openai/conversations`, `…/endpoint/sessions[/{id}[:stop]]`, `…/endpoint/protocols/invocations` | the same suffix after `/<agent>/` |
| prompt | `{project URL}/openai/v1/responses` (+ `agent_reference`) | `{gateway}/ai/<project>/<agent>/openai/v1/responses` (the gateway sets `agent_reference`) |
| prompt | `{project URL}/openai/v1/conversations` | `{gateway}/ai/<project>/<agent>/openai/v1/conversations` |

```python
# OpenAI SDK against a hosted agent through the gateway: the same code as against Foundry, another base URL
from openai import OpenAI
client = OpenAI(base_url="https://apim-agents-3471ad.azure-api.net/ai/models-demo/basic-code-agent/endpoint/protocols/openai",
                api_key=entra_token,                                   # Authorization: Bearer <token for https://ai.azure.com>
                default_headers={"Ocp-Apim-Subscription-Key": key}, default_query={"api-version": "v1"})
conv = client.conversations.create()
client.responses.create(input="My favourite colour is teal.", conversation=conv.id)
client.responses.create(input="What is my favourite colour?", conversation=conv.id)   # "teal"
```

What the gateway adds (and the only differences from a direct call):

- **Who:** your token is validated (tenant, audience); hosted agents get your identity as `x-ms-user-identity`, so
  Foundry keeps **sessions and conversations per caller** even though it sees the gateway's identity. A caller can't
  set `x-ms-user-identity` or `x-ms-user-isolation-key`.
- **Which version:** always the evaluated one. A session you create is bound to it, whatever `version_indicator` says.
- **Where:** only the agent's own endpoints; any other Foundry path gets `404 AGENT_PATH_NOT_ALLOWED`.
- **Limits and switches:** kill switches (503), rate limits and quotas (429), content safety on model calls.
- **Correlation:** `x-correlation-id` on the response = yours, else your `traceparent` trace ID, else the gateway's.

Conversation history: pass `conversation` (or `previous_response_id`). A session (`agent_session_id`) keeps the
same sandbox and its files, not the chat history.

## Add an agent

1. Copy an agent folder (prompt: `projects/proj-default/agents/leave-policy-agent`; code:
   `projects/models-demo/agents/basic-code-agent`) to `projects/<project>/agents/<new-agent>` (lowercase, digits,
   hyphens), and set its `model` and `judge_model` to the project's `<connection>/<model>`.
2. Edit `agent.yaml` (name must match the folder), the instructions or code, docs, evaluation dataset and smoke tests.
3. Open a pull request; `agent-ci` validates it. Merge to deploy.

## Add a project

Create `projects/<project>/project.yaml` (as above: `foundry.project`, `gateway.path` = `ai/<project>`, `models` with
a connection name not used by another project in the environment) and `project.md`, then add agents. Merge: onboarding
creates the Foundry project and its model access, then the agents are deployed. A project's consumers subscribe to
its product; one key covers every agent in the project.

## Version served and stopping an agent

- **Version served:** the named value `agent-version-<agent>`. Only the pipeline moves it, and only to a version
  that passed evaluation in the same run. To roll back, set it to an earlier version (or revert in Git and merge).
  Hosted agents: the agent endpoint's version selector in Foundry serves it (set by the same step); roll back by
  reverting in Git and merging, so both move together.
- **Stop an agent:** set its kill switch `ks-agent-<agent>` to `suspended` (APIM, Named values); callers get
  `503 AI_ROUTE_SUSPENDED`. The pipeline creates it once and never resets it; the smoke test skips suspended agents.
  The model and global kill switches are the gateway repository's.

## Environments

Both projects have `environment: test` (subscription `scbx-testing`): onboarding runs in the GitHub environment
`model-test`, agent-deploy in `agent-test`; APIM `apim-agents-3471ad`, agent Foundry account
`az-aif-tst-model-npr-01` (projects only; the models are in the gateway's models-only account
`aif-tst-models-3471ad`), pipeline identity `id-promptops-pipeline`, signed in with GitHub OIDC (no secrets). The
repository variable `MODEL_ENVIRONMENTS` lists the environments onboarding may run in (`sandbox,test`).

Variables and permissions: see [Pipelines](#pipelines).

The sandbox (`sandbox`, APIM `apim-sbx-bbf0o`) still has the gateway but no projects.

## Run locally

```bash
pip install -r scripts/requirements.txt
az login
# onboarding plan (state in the environment's storage: platform/environments/sandbox.yaml terraform_state)
cd onboarding/terraform
terraform init -backend-config="resource_group_name=rg-ai-gateway-sandbox" -backend-config="storage_account_name=sttfsbxbbf0o" \
  -backend-config="container_name=tfstate" -backend-config="key=ai-gateway-model-access-proj-default-sandbox.tfstate" \
  -backend-config="use_azuread_auth=true"
terraform test                                      # mocked plan tests, no Azure access
terraform plan -var environment=sandbox -var project=proj-default
cd ../..
python scripts/model_smoke.py access proj-default sandbox

# agents
export AZURE_SUBSCRIPTION_ID=... APIM_RESOURCE_GROUP=rg-ai-gateway-sandbox APIM_NAME=apim-sbx-bbf0o
python scripts/agent_pipeline.py validate
python scripts/agent_pipeline.py deploy   proj-default
python scripts/agent_pipeline.py evaluate proj-default
python scripts/agent_pipeline.py register proj-default
python scripts/agent_pipeline.py smoke    proj-default
```
