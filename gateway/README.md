# AI gateway: model onboarding and model access

Models are hosted **once**, in model-hosting Foundry accounts, and served **only through the AI gateway**
(Azure API Management). Foundry accounts that hold agents have **no model deployments**: their projects reach
models through an *AI Gateway connection*, and their agents use `"<connection>/<model>"`, e.g.
`platform-models/gpt-5.4-mini`. An organisation may use the same Foundry instance for both; the folders below
keep the two roles apart either way.

Two pipelines, owned by different teams and run at different times:

| | **Model onboarding** (P2) | **Model access** (part of use-case onboarding, P3) |
|---|---|---|
| Question | Is this model available on the platform? | Can this project use this model, and how much? |
| Change | `gateway/foundry-accounts/<account>/models/<model>/` | `models:` in `projects/<project>/project.yaml` |
| Owner | Platform team | Use-case team requests by pull request; platform team approves |
| Workflow | `.github/workflows/model-onboarding.yml` | `.github/workflows/model-access.yml`, called by `agent-deploy` **before** it deploys the project's agents |
| Terraform stack | `gateway/terraform/models` | `gateway/terraform/model-access` (one state per project) |
| State file | `ai-gateway-models-<env>.tfstate` | `ai-gateway-model-access-<project>-<env>.tfstate` |

A new model is available to nobody until a project asks for it. A project can only ask for a model that the
gateway has registered.

```
gateway/foundry-accounts/<account>/models/<model>/model.yaml           projects/<project>/project.yaml
        │  model onboarding                                                     │  model access
        ▼                                                                       ▼
  deployment in <account> ◄── APIM backend ◄── models/<project> API ◄── AI Gateway connection in the project
  (capacity, guardrail)       model-<account>-<deployment>  (token check,        "platform-models"
                              registry  model-meta-<model>   kill switch,          agents: model =
                              kill switch ks-model-<model>   token limits)         platform-models/<model>
```

## Environments

Every definition says which platform environment it belongs to, and each pipeline run targets that environment's
Azure resources through the GitHub environment of the same name:

| Definition | Field | Default |
|---|---|---|
| `gateway/foundry-accounts/<account>/account.yaml` | `environment` | required |
| `projects/<project>/project.yaml` | `environment` | `test` |

| Platform environment | Coordinates | GitHub environment (model pipelines / agent pipeline) |
|---|---|---|
| `test` | `platform/environments/test.yaml` | `model-test` / `agent-test` |
| `sandbox` (disposable, `gateway/sandbox`) | `platform/environments/sandbox.yaml` | `sandbox` / `sandbox` |

Each GitHub environment holds `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (and `APIM_NAME`,
`APIM_RESOURCE_GROUP` for the agent pipeline). The repository variable **`MODEL_ENVIRONMENTS`** (e.g. `sandbox` or
`sandbox,test`) lists the environments the model pipelines may run in: add an environment only once its GitHub
environment and pipeline identity are set up. `scripts/matrix.py` builds the job matrices from these fields.

## Developer Portal (APIM)

| What developers see | Where it comes from |
|---|---|
| Product **Platform models**: catalog of every model (summary, status, region, content safety, capacity, name to use in an agent) | model onboarding: the first paragraph of each `docs.md` (stored in the registry as `summary`) |
| API **Models - &lt;project&gt;**: the project's models with its limits, and the chat completions reference (parameters, request example, error codes) | model access: OpenAPI spec generated from the registry and `project.yaml` |

Both pipelines publish the portal after each apply (`gateway/scripts/publish_portal.py`). The product is visible to
the **Developers** group (signed-in users). A new APIM instance's portal must be opened once from the Azure portal
(APIM > Developer portal) before it can be published; until then the script warns instead of failing.

## Single source of truth: the gateway

The **model registry is in APIM**, not in Git: model onboarding writes one named value `model-meta-<model>` per
model, and everything that needs to know which models exist reads it from APIM:

- the model-access stack (validation, backend, deployment name, capacity) through `azapi_resource_list`;
- the smoke tests (`gateway/scripts/model_smoke.py`);
- project repositories created from the template (they have no `gateway/` folder at all).

Git holds what the platform team *wants* registered; APIM holds what *is* registered, and only the pipelines
write it. A use-case team types only a model name and its limits.

| Named value in APIM | Value | Written by | Read by |
|---|---|---|---|
| `model-meta-<model>` | JSON: name, api, status, format, model, version, hosting account, region, deployment, `capacity_tpm`, guardrail, backend ID, kill switch | model onboarding | model access, smoke tests, project pipelines |
| `ks-model-<model>` | `active` or `suspended` | model onboarding (once); then operations only | the project model API policies |
| `model-limit-<project>-<model>` | JSON: project, model, `tokens_per_minute`, `tokens_per_day` | model access | model access of every other project (free capacity) |

APIM names allow no dots, so `<model>` in these names is the model name with dots replaced by hyphens
(`gpt-5.4-mini` → `gpt-5-4-mini`). The registry entry keeps the real name.

## Model onboarding

### Add or change a model

1. Create `gateway/foundry-accounts/<account>/models/<model>/` (or edit it):

   | File | What it is |
   |---|---|
   | `model.yaml` | gateway model name (= folder name), API, status, model (format, name, version), deployment (name, SKU, capacity, version upgrade, `existing`), guardrail |
   | `docs.md` | model card for the developer portal |
   | `tests.yaml` | smoke tests run through the gateway by an agent that uses the model |

2. Open a pull request: `model-onboarding` checks format, validates, runs the plan tests and shows the plan.
3. Merge to main: plan, **approval** on the `model-test` environment, apply, smoke test.

A new hosting account (e.g. another region for Claude Sonnet) is a new folder
`gateway/foundry-accounts/<account>/` with `account.yaml` (subscription, resource group, region, allowed model
formats). Only folders under `gateway/foundry-accounts/` can receive models, so a model can never be deployed
into an agent account.

### What it creates per model

| Resource | Name | Notes |
|---|---|---|
| Model deployment | `deployment.name` in the hosting account | SKU, capacity, guardrail (`raiPolicyName`), version upgrade. `existing: true` adopts a deployment made before this pipeline (Terraform `import`) instead of recreating it |
| APIM backend | `model-<account>-<deployment>` | `https://<account>.cognitiveservices.azure.com/openai` |
| Registry entry | named value `model-meta-<model>` | see above |
| Kill switch | named value `ks-model-<model>` | created `active`; later changes are never overwritten by an apply |
| Product | `platform-models` (from the environment file) | catalog of every model in its description; visible to the Developers group; every project's model API is added to it |

### Validation (fails the plan before anything changes)

- `model.yaml` `name` equals its folder name; names are lowercase letters, digits, dots and hyphens.
- The model's format is one of its hosting account's `formats`.
- `api` is `openai`; `status` is `active` or `deprecated`.

## Model access for a project

### Request a model

In `projects/<project>/project.yaml`:

```yaml
models:
  connection: platform-models          # AI Gateway connection created in the project
  allowed:
    - name: gpt-5.4-mini               # a gateway model name from the registry
      tokens_per_minute: 20000         # this project's share of the model's capacity
      tokens_per_day: 2000000
```

Agents then set `model: platform-models/gpt-5.4-mini` (and the evaluation judge `judge_model` the same way):
`agent-ci` rejects any other model in a project that has a `models:` section.

Open a pull request: `model-access` plans the change **against the live registry**, and `agent-ci` validates the
agents. Merge to main: `agent-deploy` first calls `model-access` for the project (approval, apply, smoke test), so
the connection and limits exist before the agents that use them are deployed. If model access fails, the agents
aren't deployed. A change to the model-access stack itself re-runs every project that lists models.

### What it creates per project

| Resource | Name | Notes |
|---|---|---|
| Model API | `models-<project>`, path `models/<project>` | imported OpenAPI: `POST /deployments/{model}/chat/completions`, description with the project's models and limits (Developer Portal); no subscription key: the caller's Entra token is checked |
| API policy | from `terraform/model-access/policies/project-models-api.xml.tftpl` | see below |
| Product link | `platform-models` | |
| Allocation | named value `model-limit-<project>-<model>` per model | read by other projects' capacity check |
| AI Gateway connection | `models.connection` in the Foundry project | category `ApiManagement`, auth `ProjectManagedIdentity` (audience `https://cognitiveservices.azure.com`), target `https://<apim>/models/<project>`, `deploymentInPath: "true"`, static model list = the allowed models |

### Gateway policy, in order

1. **Only this project.** `validate-azure-ad-token`: audience `https://cognitiveservices.azure.com`, and the
   token's `xms_mirid` claim must be the project's resource ID. Any other caller gets `401`.
2. **Allowed models only.** Any model not in `models.allowed` gets `404 MODEL_NOT_ALLOWED`.
3. **Kill switch.** If `ks-model-<model>` is not `active`: `503 AI_ROUTE_SUSPENDED`, for every project at once.
4. **Token limits.** `llm-token-limit` per project and model: `tokens_per_minute` and a daily `token-quota`
   (`429` when exceeded, header `x-ratelimit-remaining-tokens`).
5. **Route.** Backend `model-<account>-<deployment>`, deployment name from the registry.
6. **Authenticate to the model** with APIM's own managed identity. The project's token is never forwarded, so
   projects need no access to the hosting accounts.

### Validation (fails the plan before anything changes)

- At least one model is listed; `tokens_per_minute > 0` and `tokens_per_day >= tokens_per_minute`.
- Every listed model is in the gateway registry. The error lists what *is* registered.
- **Capacity:** the other projects' allocations plus this request fit in the model's `capacity_tpm`. The error
  says how much is free.

## Kill switch, rollback, retirement

- **Stop a model for everyone:** APIM → Named values → `ks-model-<model>` → `suspended`. Callers get
  `503 AI_ROUTE_SUSPENDED`. No pipeline run re-enables it.
- **Stop one project's access:** remove the model from its `models.allowed` (or set a very low limit) and merge.
- **Roll back a model change:** revert the commit and merge; the plan shows the change back.
- **Retire a model:** set `status: deprecated` (still served, visible in the registry and smoke test), ask the
  projects to move, then delete the folder once no `model-limit-*-<model>` remains.

## Workflows and triggers

| Change | What runs |
|---|---|
| `gateway/foundry-accounts/**`, `gateway/terraform/models/**`, `platform/environments/**` | `model-onboarding` (PR: checks + plan; main: apply + smoke) |
| `projects/<project>/project.yaml` | PR: `agent-ci` + `model-access` plan. Main: `agent-deploy` → `model-access` (apply, smoke, portal) → agent deploy, evaluation, gateway |
| `projects/<project>/agents/**` | `agent-ci` / `agent-deploy` (agents only) |
| `gateway/terraform/model-access/**` | `model-access` for every project with a `models:` section |

## Identity and permissions

| Who | Role | Scope |
|---|---|---|
| Pipeline identity (GitHub OIDC) | Foundry Account Owner (deploy models, set guardrails) | each hosting account |
| | API Management Service Contributor | the APIM instance |
| | Foundry User + Contributor (create connections) | the agent Foundry account / its resource group |
| | Storage Blob Data Contributor | the Terraform state container |
| APIM managed identity | Foundry User (call the deployments) | each hosting account |
| Foundry project managed identity | none on the hosting accounts | it only calls APIM |

GitHub environments `model-test` (model pipelines) and `agent-test` (agent pipeline), with required reviewers; variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`. Everything else comes from `platform/environments/<env>.yaml`.

## Run locally

```bash
az login
cd gateway/terraform/models
terraform init -backend-config="resource_group_name=rg-scbx-terraform-state" \
  -backend-config="storage_account_name=stscbxtf3471ad5a" -backend-config="container_name=tfstate" \
  -backend-config="key=ai-gateway-models-test.tfstate" -backend-config="use_azuread_auth=true"
terraform test                         # plan tests with mocked providers, no Azure access
terraform plan -var environment=test

cd ../model-access                     # same init with key ai-gateway-model-access-<project>-test.tfstate
terraform plan -var environment=test -var project=proj-default

python gateway/scripts/model_smoke.py models test
python gateway/scripts/model_smoke.py access proj-default test
python gateway/scripts/publish_portal.py test
```

## Sandbox

`gateway/sandbox` creates a disposable copy of the platform in one resource group: model-hosting Foundry account,
agent-only Foundry account with project `models-demo`, APIM Basic v2, Terraform state storage, and the pipeline
identity `id-sbx-pipeline` trusted by the GitHub environment `sandbox`. `platform/environments/sandbox.yaml`,
`gateway/foundry-accounts/aif-sbx-models-*/` and `projects/models-demo/` point at it. `terraform destroy` in
`gateway/sandbox` removes everything; then delete those three paths and the `sandbox` GitHub environment, and
remove `sandbox` from `MODEL_ENVIRONMENTS`.

## Tested end to end (sandbox, 13 Nov 2026)

In `gateway/sandbox` (separate hosting and agent-only Foundry accounts, APIM Basic v2), environment `sandbox`:

| Check | Result |
|---|---|
| Model onboarding: deployment, backend, registry, kill switch, product (5 resources) | created; smoke test: deployment Succeeded, backend registered, kill switch active |
| Model access for `models-demo`: plan reads the registry from APIM, checks capacity (20,000 of 50,000 TPM) | 7 resources created: model API, policy, product link, allocation, AI Gateway connection |
| Prompt agent in the agent-only project with `model: platform-models/gpt-5.4-mini` | answers through the gateway |
| Another identity calling the project's model API | refused (401) |
| Kill switch `ks-model-gpt-5-4-mini` = suspended | the agent's call fails with `503 AI_ROUTE_SUSPENDED`, passed through by Foundry |
| Agent pipeline: deploy + evaluation gate with `judge_model: platform-models/gpt-5.4-mini` | passed (relevance 3/3, coherence 3/3) |

## Limits and notes

- Connected models (AI Gateway connections) are documented by Microsoft for **prompt agents**; hosted agents
  call the project's model API directly with their own identity. The connection type `ApiManagement` is in
  preview and can only be created through code.
- **Guardrails** set on an agent don't apply to connected models: content safety is the guardrail on the
  deployment in the hosting account (`guardrail` in `model.yaml`).
- **Evaluations work with a connected model as the AI judge** (`judge_model: platform-models/<model>`): verified in
  the sandbox, where the agent account has no deployments at all. Other features that may need a model in the
  agent's own account (file search embeddings, memory, Foundry IQ) still need checking before relying on them.
- `api: anthropic` (Claude, Messages route) is not supported yet: the model API has only the chat completions
  operation.
