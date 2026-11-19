# Agent deployment (project repository)

Onboards use cases onto the AI platform and deploys their agents. Each project gets **its own Microsoft Foundry
project** and governed access to platform models (use-case onboarding); then its agents are deployed, gated by a
Foundry evaluation, and published on the AI gateway (Azure API Management) with documentation in the Developer Portal.

The platform itself lives in the gateway repository, **[ai-gateway](https://github.com/DeepuMM/ai-gateway)**: the
gateway, model hosting, and the shared gateway policy that every project uses (kill switches, content safety,
routing, limits). See its README for the APIM structure, the policy fragments and how the kill switches work.

## Pipelines

Three pipelines, one per kind of deployment, each its own GitHub Actions workflow and its own run. All sign in to
Azure with GitHub OIDC (no secrets).

| Pipeline | Deploys | Unit (one item, one environment) | GitHub environment | Terraform state |
|---|---|---|---|---|
| **tool-deploy** | each changed project-owned tool on the gateway (API `tools/<project>/<tool>/mcp`, auth guard from `shared_with`, kill switch, registry entry), then the tool tests: another identity refused, the owner's tests and misuse tests. See [Tools](#tools-project-owned-mcp-servers) | `_tool.yml` | `model-<env>` | `tool-<project>-<tool>-<env>.tfstate` |
| **use-case-onboarding** | each project whose `project.yaml` changed: the Foundry project, its model API (limits, content safety), its allocation, the AI Gateway connection, a connection per tool in `tools.use`; then a temporary agent answers through each model, another identity is refused, and the Developer Portal is published | `_project.yml` | `model-<env>` | `ai-gateway-model-access-<project>-<env>.tfstate` |
| **agent-deploy** | each project with agent changes: validate; new Foundry versions for changed agents (hosted: image built in the environment's registry); **evaluation gate** (every evaluator at `min_pass_rate`, else the gateway keeps the previous version); register the agent API `ai/<project>` pinned to the evaluated versions, kill switches, product (and lists the agent roles callers need); Developer Portal; smoke tests through the gateway with each agent's access checks (and the wrong credentials refused) | `_agents.yml` | `agent-<env>` | none (APIM and Foundry are the record) |
| `ci` | checks on every pull request and merge, no Azure: every definition valid, hosted agents compile, Terraform plan tests | — | — | — |

Every pipeline has the same shape: `plan ─► (wait ─►) dev ─► uat ─► prd`, one box per tool or project in each stage.

```
.github/workflows/
  tool-deploy.yml            plan ─► dev ─► uat ─► prd                         (dev / deploy models-demo/ms-learn)
  use-case-onboarding.yml    plan ─► wait for tool-deploy ─► dev ─► uat ─► prd  (dev / onboard models-demo)
  agent-deploy.yml           plan ─► wait for both ─► dev ─► uat ─► prd         (dev / release agents models-demo)
  ci.yml
  _tool.yml, _project.yml, _agents.yml     the units (shared templates)
.github/actions/setup, tf-init             shared steps (Python, Terraform, Azure sign-in, state backend)
```

### Triggers

| Event | tool-deploy | use-case-onboarding | agent-deploy |
|---|---|---|---|
| pull request | plans for the changed tools | plans for the changed projects | validation of the changed projects |
| merge to `main` | dev: `projects/<p>/tools/<t>/**` (all tools when `tool-deploy/` changed) | dev: `projects/<p>/project.yaml` (all projects when `onboarding/` changed) | dev: anything else under `projects/<p>/` (all projects when `templates/` changed) |
| release tag `v*` | everything: dev, then uat, then prd | everything: dev, uat, prd | everything: dev, uat, prd |
| manual run | everything, dev | everything, dev | everything, dev |

`plan` (`scripts/pipeline_plan.py`) lists, per environment, what to deploy: every stage the platform offers
(`PLATFORM_ENVIRONMENTS`), minus any a project leaves out with `environments:` in its `project.yaml`.

### What one merge starts

One push to `main` starts **at most one run of each pipeline**, never one run per project. Inside a run, each stage
is a **matrix with one box per changed item**: per project for use-case-onboarding and agent-deploy, per tool for
tool-deploy. The boxes of a stage run one at a time (`max-parallel: 1`, so APIM and the Developer Portal are never
written by two at once) and independently (`fail-fast: false`: one project failing doesn't stop the others; the
next stage starts only if every box succeeded).

| The merge changes | tool-deploy | use-case-onboarding | agent-deploy |
|---|---|---|---|
| `projects/a/agents/x/instructions.md` | — | — | 1 run: dev [a] |
| `projects/a/agents/**` and `projects/b/agents/**` | — | — | 1 run: dev [a, b] |
| `projects/a/project.yaml` | — | 1 run: dev [a] | 1 run: dev [a], after onboarding |
| `projects/a/project.yaml` and `projects/b/project.yaml` | — | 1 run: dev [a, b] | 1 run: dev [a, b], after onboarding |
| `projects/a/tools/t/tool.yaml` | 1 run: dev [a/t] | — | — |
| `projects/a/tools/t/**`, `projects/a/project.yaml` (`tools.use`) and `projects/a/agents/**` | 1 run: dev [a/t] | 1 run: dev [a], after tool-deploy | 1 run: dev [a], after both |
| `onboarding/terraform/**` | — | 1 run: dev [every project] | — |
| `templates/**` | — | — | 1 run: dev [every project] |
| `tool-deploy/**` or `scripts/tool_smoke.py` | 1 run: dev [every tool] | — | — |
| `README.md`, `scripts/agent_pipeline.py` | — | — | — (only `ci`) |
| release tag `v1.2.0` | 1 run: dev, uat, prd [every tool] | 1 run: dev, uat, prd [every project] | 1 run: dev, uat, prd [every project] |

`[...]` is the matrix of the dev stage; uat and prd have the same shape on a release tag, limited to projects that
list them. A change to `project.yaml` re-onboards **and** re-releases the project's agents (agent-deploy also
watches `project.yaml`: limits, gateway path and docs live there); agent versions are only created for agents whose
definition changed.

### Order between the pipelines

A merge that changes a tool, a `project.yaml` and an agent starts all three pipelines at once. They still deploy in
order, **tool-deploy ─► use-case-onboarding ─► agent-deploy**: onboarding starts with a `wait` job that waits for this
commit's tool-deploy run, and agent-deploy waits for both (`scripts/wait_for_runs.py`). A pipeline waits only for runs
the commit actually triggered, and stops if one of them failed, so a project's connections always exist before its
agents are deployed and a tool is published before projects connect to it. Inside a pipeline the order is set by
`needs` (dev ─► uat ─► prd), never by matrix order; the matrix only fans out over tools or projects of one stage.

**When a run stops, nothing has changed yet** if it stops in a plan: a model isn't registered or has no capacity left,
a tool isn't published or isn't shared with the project, a shared fragment is missing on the gateway, names collide,
content safety is looser than the platform, or a definition is invalid. Later failures (Azure refuses a change, an
evaluation or a test fails) leave the previous agent versions served.

### Stages and promotion

Three stages, in promotion order: **dev → uat → prd**. Which of them run is set in one place, the **repository
variable `PLATFORM_ENVIRONMENTS`** (comma-separated; today `dev`). Every project deploys to every stage the variable lists, so
turning uat and prd on later is one variable change (`dev,uat,prd`), not an edit to every project.

A project normally says nothing about environments. Only to hold one back, `project.yaml` can **restrict** the
stages (it can leave stages out, never add one the platform doesn't offer):

```yaml
environments: [dev]                        # optional: e.g. a new project not ready for uat and prd yet
```

| Stage | Runs on | Approval | GitHub environments |
|---|---|---|---|
| **dev** | every merge to `main`, manual runs, release tags | none | `model-dev`, `agent-dev` |
| **uat** | release tags `v*`, after the pipeline's dev stage for that commit succeeded | required reviewers | `model-uat`, `agent-uat` |
| **prd** | release tags `v*`, after uat succeeded | required reviewers | `model-prd`, `agent-prd` |

- A tag promotes **exactly the tagged commit**: each pipeline runs dev, then uat after approval, then prd after
  approval, and the pipelines keep their order (agent-deploy waits for onboarding, which waits for tool-deploy).
  Every box of a stage must succeed before the next stage starts.
- Approval is GitHub's environment protection: each job of uat and prd waits until a reviewer approves it in the
  run (Actions > the run > Review deployments).
- Each environment has its own gateway, registry, kill switches, Foundry account and Terraform state, so a stage
  can't affect another. Agents are evaluated again in each environment (Foundry versions belong to an account), and
  tools are published in an environment before projects there connect to them.
- **Today** `PLATFORM_ENVIRONMENTS` is `dev`. uat and prd exist as GitHub environments (with approval), with the same
  coordinates as dev until their own resources exist; add them to the variable once they do.

### Environment settings: GitHub variables

Nothing about an environment is kept in this repository. Each GitHub environment holds its coordinates as
**variables** (not secrets); the jobs pass them to Terraform (`TF_VAR_*`) and to the scripts
(`scripts/platform_env.py`). Both GitHub environments of an environment (`model-<env>`, `agent-<env>`) carry the same
set, written by `scripts/set_github_environment.sh <env> KEY=VALUE ...`:

| Variable | Meaning | dev (uat and prd: the same, for now) |
|---|---|---|
| `AZURE_CLIENT_ID` | pipeline identity (set per GitHub environment, with its federated credential) | `id-promptops-pipeline` |
| `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | sign-in | the platform tenant, `scbx-testing` |
| `APIM_SUBSCRIPTION_ID`, `APIM_RESOURCE_GROUP`, `APIM_NAME` | the AI gateway | `apim-agents-3471ad` |
| `MODELS_PRODUCT` | product of model and tool APIs | `platform-models` |
| `FOUNDRY_SUBSCRIPTION_ID`, `FOUNDRY_RESOURCE_GROUP`, `FOUNDRY_ACCOUNT` | the agent Foundry account | `az-aif-tst-model-npr-01` |
| `TF_STATE_RESOURCE_GROUP`, `TF_STATE_STORAGE_ACCOUNT`, `TF_STATE_CONTAINER` | Terraform state | `stscbxtf3471ad5a` / `tfstate` |
| `PORTAL_ANONYMOUS` | `true`: products visible in the portal without sign-in | `false` |
| `ACR_NAME` (`agent-<env>`) | container registry for hosted agents' images | `acrscbx3471ad` |
| `AGENTS_APP_ID` | client ID of the gateway's agent app (Entra; ai-gateway `scripts/setup_agents_app.py`): agent roles, callers' token scope `<AGENTS_APP_ID>/.default` | `AI Gateway Agents (dev)` |

Repository variable (not per environment): **`PLATFORM_ENVIRONMENTS`**, the stages the platform offers, in promotion
order (`dev`; later `dev,uat,prd`). See [Stages and promotion](#stages-and-promotion).

Derived, not set: the gateway URL `https://<APIM_NAME>.azure-api.net`, a Foundry project's URL
`https://<FOUNDRY_ACCOUNT>.services.ai.azure.com/api/projects/<foundry.project>`, an agent's base URL
`<gateway URL>/ai/<project>/<agent>/`, a project's model API `<gateway URL>/models/<project>`, a tool's endpoint
`<gateway URL>/tools/<owner>/<tool>/mcp`.

### Permissions

The pipeline identity (one per environment) needs:

| Role | Scope | For |
|---|---|---|
| **Foundry Project Manager** (or Contributor) | the agent Foundry account | create the Foundry project and its connections (onboarding; temporary connection in tool tests) |
| **Foundry User** | the agent Foundry account | agent versions, evaluation runs, the smoke-test agent (onboarding, tool tests, agents) |
| **API Management Service Contributor** | the APIM instance | model, tool and agent APIs, named values, products, portal |
| Role **`agents.all`** | the gateway's agent app (Entra) | smoke tests call every agent. Assigned once per environment by ai-gateway `scripts/setup_agents_app.py`. The pipeline has **no Entra rights**: agent roles are created by an owner of the agent app (`roles`, below) |
| **Storage Blob Data Contributor** | the Terraform state container | onboarding and tool state |
| Federated credential | the identity, trusting `repo:<owner>@<id>/agent-deployment@<id>:environment:<env>` | GitHub OIDC sign-in |

Set up by the gateway repository, not here: APIM's managed identity has **Foundry User** on the agent account (so the
gateway can call the agents) and the custom role **Foundry Agent User Identity Impersonation** there (so it can pass
the caller's identity to hosted agents); the Foundry projects' own identities need no role (they only call the
gateway). In test, `id-promptops-pipeline` has Contributor on the resource group, which covers the Contributor-level
rows (see the gateway repository's `gateway/test-setup/`).

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
| `projects/<project>/agents/<agent>/agent.yaml` | name, kind (prompt or hosted), description, owner, model (`<connection>/<model>`), agent limits; hosted: code, size, protocols, environment |
| `projects/<project>/agents/<agent>/instructions.md` | the agent's instructions (prompt agents; optional for hosted agents) |
| `projects/<project>/agents/<agent>/src/` | hosted agents: the code, its dependency files, `Dockerfile` and `.dockerignore` (`container.source_dir`) |
| `projects/<project>/agents/<agent>/env/<environment>.env` | hosted agents: settings for one environment (environment variables, no secrets); `*.env.sample` files are ignored samples |
| `projects/<project>/agents/<agent>/docs.md` | operation documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/evals.yaml` | evaluation gate: judge model, minimum pass rate, evaluators, dataset |
| `projects/<project>/agents/<agent>/tests.yaml` | smoke tests (`input`, `must_contain_any`) |
| `projects/<project>/tools/<tool>/tool.yaml` | a tool the project owns (MCP server): backend, exposed tools and their impact, rate, `shared_with`; see [Tools](#tools-project-owned-mcp-servers) |
| `projects/<project>/tools/<tool>/docs.md`, `tests.yaml` | the tool's page for agent builders; the owner's tests (`expect: called` and misuse tests `expect: refused`) |
| `onboarding/terraform` | use-case onboarding stack (state `ai-gateway-model-access-<project>-<env>.tfstate`) |
| `templates/` | the agent operation and project API policies: each agent's and project's own values, plus the shared fragments by name |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `evaluate`, `register`, `smoke` |
| `demo/agent-access-app/` | local web app demo: call agents with your gateway or Foundry token and compare ([README](demo/agent-access-app/README.md)) |
| `postman/` | demo collection: calling agents with each access list, the refusals, an agent calling an agent ([postman/README.md](postman/README.md)) |
| `scripts/model_smoke.py` | onboarding smoke test (`access`) |
| `scripts/tool_smoke.py` | tool tests for `tool-deploy` (auth guard, owner's tests, misuse tests) |
| `tool-deploy/terraform` | tool deployment stack (state `tool-<project>-<tool>-<env>.tfstate`) |
| `.github/workflows/` | `tool-deploy.yml`, `use-case-onboarding.yml`, `agent-deploy.yml`, `ci.yml`, and the units `_tool`, `_project`, `_agents` |
| `.github/actions/` | shared steps: `setup` (Python, Terraform, Azure sign-in), `tf-init` (state backend) |
| `scripts/pipeline_plan.py`, `scripts/wait_for_runs.py` | what each pipeline deploys per environment; the order between the pipelines |
| `scripts/platform_env.py`, `scripts/set_github_environment.sh` | the environment from GitHub variables; setting them |

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

**Rules** (checked on the pull request by `ci` and the project's plan, and by the onboarding plan, before anything changes):

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
Safety call per 1,000 characters and adds latency, and when Content Safety is unavailable the answer is stopped too
(the check fails closed). Foundry streams its model calls, so a flagged answer isn't replaced by a `403`: the stream stops at the first
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

`proj-default` sets `violence: 2`; `models-demo` uses the platform defaults. Neither checks answers. The gateway's
Content Safety check fails closed: if the service is unavailable, requests get the same `403` as a real block (see
"Availability" in the gateway repository's README for how to tell them apart).

**Proof on every onboarding run.** The onboarding smoke test sends one fixed prompt, "A soldier was wounded by gunfire
during the battle." (Violence severity 3, every other category 0), through a temporary agent in the project. A
category is blocked at severity >= its threshold, so the same prompt must be **blocked** in `proj-default` (threshold
2) and **answered** in `models-demo` (platform default 4); either result being wrong fails the run. The step summary
shows the expected and actual result per project.

## Projects in the dev environment

| Project | Foundry project | Connection | Agent API | Tokens per minute | Content safety |
|---|---|---|---|---|---|
| `models-demo` | `models-demo` (created by onboarding) | `platform-models` | `ai/models-demo` | 20,000 of `gpt-5.4-mini`'s 200,000 | platform defaults |
| `proj-default` | `proj-default` (adopted: existed before onboarding) | `proj-default-models` | `ai/proj-default` | 20,000 | `violence: 2` |

Agents: `models-demo`: `demo-assistant` (prompt), `basic-code-agent` (hosted, code); `proj-default`:
`expense-policy-agent`, `leave-policy-agent` (prompt).

## Agent kinds

| | Prompt agent (`kind: prompt`, default) | Hosted (code) agent (`kind: hosted`) |
|---|---|---|
| What it is | a model + instructions, run by Foundry | your code (e.g. Microsoft Agent Framework) in a container image, run by Foundry |
| Folder | `agent.yaml`, `instructions.md`, `docs.md`, `tests.yaml`, `evals.yaml` | the same (instructions optional) + `src/` with the code, its dependency files, a `Dockerfile` and a `.dockerignore` |
| Example | `proj-default/agents/leave-policy-agent` | `models-demo/agents/basic-code-agent` (Foundry sample `hosted-agents/agent-framework/responses/01-basic`) |
| Model | `model: <connection>/<model>` | the same; the pipeline gives it to the code as `AZURE_AI_MODEL_DEPLOYMENT_NAME`, so model calls go through the gateway like every agent's |
| Deploy | new version when instructions/model change | **container** (default): the pipeline builds `src/Dockerfile` with ACR's cloud build into the environment's container registry (`ACR_NAME`) as `<registry>/<project>/<agent>:<content hash>` (unchanged code, same tag, no rebuild) and creates the version from that image. **code** (alternative): the pipeline zips `src/`, Foundry installs the dependencies and builds it |
| Version served | `agent_reference` set by the gateway | the agent endpoint's version selector, 100% to the evaluated version. Before each upload the pipeline pins it to the version that is live (never a failed build): Foundry's default `@latest` would serve new code before evaluation |
| Protocols | Responses | `responses` (required: evaluation and smoke tests use it), optionally `invocations` |

A hosted agent's `agent.yaml` (see `projects/models-demo/agents/basic-code-agent/agent.yaml`):

```yaml
kind: hosted
model: platform-models/gpt-5.4-mini       # reaches the code as AZURE_AI_MODEL_DEPLOYMENT_NAME
container:                                 # image built from src/Dockerfile in the environment's registry
  source_dir: src
  dockerfile: Dockerfile
cpu: "0.5"                                 # with memory: 0.25/0.5Gi, 0.5/1Gi, 1/2Gi, 2/4Gi
memory: 1Gi
protocols:
  - {protocol: responses, version: 2.0.0}
environment: {PYTHONNOUSERSITE: "1"}      # no secrets (checked): use Key Vault with the agent's identity
```

Container image, registry and access: [Hosted agents as container images (ACR)](#hosted-agents-as-container-images-acr).

### Hosted agents as container images (ACR)

A hosted agent with `container:` is shipped as an image in the environment's **Azure Container Registry**. One
registry per environment serves every hosted agent of every project in it.

| | dev (today) |
|---|---|
| Registry | `acrscbx3471ad` (`acrscbx3471ad.azurecr.io`), Basic SKU, admin user off, classic (non-ABAC) permission mode, same resource group and region as the agent Foundry account |
| Where the pipeline finds it | GitHub variable `ACR_NAME` on `agent-<env>` (uat and prd: the same registry until they get their own) |
| Image name | `<registry>.azurecr.io/<project>/<agent>:<tag>`, tag = the first 16 characters of the SHA-256 of the agent's `src/` (the files that go into the build context) |
| Example | `acrscbx3471ad.azurecr.io/models-demo/basic-code-agent:aeea268412fa6eee` |

**What agent-deploy does for a container agent:**

1. Computes the tag from `src/`. If that tag is already in the registry, nothing is built: unchanged code is never
   rebuilt, and a re-run reuses the image.
2. Otherwise builds `src/Dockerfile` with **ACR's cloud build** (`az acr build`: the build runs in Azure, no Docker
   on the runner) and pushes the image.
3. Creates the agent version from the image (`container_configuration.image`) and records the image in the version's
   metadata, so every version names its exact image.
4. Waits until Foundry has started it, then the evaluation gate, the gateway switch and the smoke tests, as for every
   agent. The previous version keeps serving until the new one passes.

**The agent's files** (see `projects/models-demo/agents/basic-code-agent/`):

```yaml
# agent.yaml
kind: hosted
model: <connection>/<model>              # reaches the code as AZURE_AI_MODEL_DEPLOYMENT_NAME
container:
  source_dir: src                        # the build context
  dockerfile: Dockerfile                 # in source_dir
cpu: "0.5"                               # with memory: 0.25/0.5Gi, 0.5/1Gi, 1/2Gi, 2/4Gi
memory: 1Gi
protocols:
  - {protocol: responses, version: 2.0.0}
environment: {PYTHONNOUSERSITE: "1"}    # no secrets (checked)
```

- `src/Dockerfile`: builds the agent and starts its server on **port 8088** (the agent server's default). The
  example uses the Foundry sample's Dockerfile: `uv` installs the locked dependencies, the code is byte-compiled at
  build time, `CMD` runs `main.py`.
- `src/.dockerignore` (required, checked by `ci`): keeps `.env`, credentials, virtual environments and caches out of
  the image.
- `src/pyproject.toml` and `uv.lock` (or `requirements.txt`): the dependencies.

### Settings of a hosted agent (environment variables)

A hosted agent reads its settings as ordinary **environment variables**, whatever its language: Python
`os.environ["LOG_LEVEL"]`, C# `Environment.GetEnvironmentVariable("LOG_LEVEL")` or `IConfiguration` (nested keys as
`Section__Key`), Node.js `process.env.LOG_LEVEL`, Java `System.getenv("LOG_LEVEL")`. No `.env` file is read inside
the container and none is in the image (`.dockerignore` keeps them out): the image is built once and promoted
unchanged, and each environment's values are set on that environment's agent version. They come from three places,
lowest precedence first:

| Source | Applies to | Example |
|---|---|---|
| `agent.yaml` `environment:` | every environment | `PYTHONNOUSERSITE: "1"` |
| `agents/<agent>/env/<environment>.env` (dotenv: `KEY=VALUE`, `#` comments, optional quotes; `*.env.sample` files are ignored, e.g. `uat.env.sample` until uat is in use) | that environment only | `env/dev.env`: `LOG_LEVEL=INFO`; `env/prd.env`: `LOG_LEVEL=WARNING` |
| **set by the platform** (agent-deploy), can't be overridden | every environment, with that environment's values | see below |

| Platform variable | Value (dev, models-demo) |
|---|---|
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | `platform-models/gpt-5.4-mini` (agent.yaml `model`: the model through the gateway) |
| `PLATFORM_ENVIRONMENT` | `dev` |
| `PLATFORM_PROJECT` | `models-demo` |
| `AI_GATEWAY_URL` | `https://apim-agents-3471ad.azure-api.net` |
| `AI_GATEWAY_PROJECT_URL` | `https://apim-agents-3471ad.azure-api.net/ai/models-demo` (the project's agent API) |
| `AI_GATEWAY_AGENTS_SCOPE` | `<AGENTS_APP_ID>/.default`: token scope for calling other agents through the gateway ([Agents calling agents](#agents-calling-agents)) |
| `FOUNDRY_PROJECT_ENDPOINT` | set by Foundry in the container (the project the agent runs in), with the agent's managed identity |

Model calls never need the gateway URL: the agent calls its project with `AZURE_AI_MODEL_DEPLOYMENT_NAME`, and the
project's connection routes them through the gateway. The gateway variables are for links, telemetry and calling the
project's own agent API.

Every variable ends up in the version's definition in Foundry, so a version shows exactly what it ran with. A change
to `agent.yaml` `environment` or to an `env/<environment>.env` file creates a new version in that environment, which
goes through the evaluation gate like any change. `ci` checks every file: valid lines and names, no secret-looking
names (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CONNECTION_STRING`), no platform variable, and no file for a stage
that isn't dev, uat or prd or that the project's `environments` leaves out. **Secrets** stay out of Git: one Key Vault per environment, read by the agent
with its managed identity (C#: `AddAzureKeyVault`; Python: `azure-keyvault-secrets`).

**Prompt agents** have no environment variables: they have no code. Everything platform-related reaches them through
their definition (the model and tool connections route through the gateway).

**Access to the registry:**

| Identity | Needs | Granted by |
|---|---|---|
| Pipeline identity | build and push | its Contributor role on the registry's resource group covers both, because the registry uses the classic permission mode (an ABAC-mode registry would need Container Registry Repository Writer instead) |
| Each **Foundry project's managed identity** that runs hosted agents | **AcrPull** on the registry (Foundry pulls the image as the project) | the platform team, once per project (the pipeline can't grant roles); today models-demo and proj-default |

**Troubleshooting:**

| Symptom | Cause | Fix |
|---|---|---|
| `image build failed in <registry>` | the Dockerfile doesn't build | the build log is in the run; build locally with `az acr build -r <registry> -t test/<agent>:local -f src/Dockerfile src` |
| version `failed` in Foundry, pipeline stops before the gateway | the project's identity can't pull (no AcrPull), or the container doesn't start on port 8088 | grant AcrPull; check the agent's logs in the Foundry portal |
| `Missing environment variable ACR_NAME` | `ACR_NAME` not set on `agent-<env>` | `scripts/set_github_environment.sh <env> ... ACR_NAME=<registry>` |

A hosted agent can also use `code:` instead of `container:` (`source_dir`, `runtime` python_3_11/12/13,
`entry_point`, `dependency_resolution: remote_build`): the pipeline zips the folder and Foundry builds it, with no
registry. Container is the default for new hosted agents: the image is built once, versioned by content and
reproducible.

`ci` checks hosted agents too: exactly one of `container` or `code`, the Dockerfile and `.dockerignore` (container)
or runtime and entry point (code), size, protocols, no secret-looking environment variables, and that the code
compiles.

## Tools (project-owned MCP servers)

A **tool** is an MCP server that agents call. It belongs to the **project** that defines it, in
`projects/<project>/tools/<tool>/`, and that project's team owns it: they describe it, decide which of its MCP tools
agents may call, test it, grant the gateway access to its backend, and decide which Foundry projects may use it. The
gateway sits between every agent and every tool, so the platform controls stay in one place: who may call, kill
switch, rate limit, which MCP tools can be called.

```
 owner's repository                       AI gateway                              consumer's repository (any)
 projects/<owner>/tools/<tool>/ ─tool-deploy─► tools/<owner>/<tool>/mcp          project.yaml tools.use: [<owner>/<tool>]
                                           │   auth guard: owner + shared_with        │ use-case-onboarding reads the
                                           └─► registry tool-meta-<owner>-<tool> ◄────┘ registry, creates the connection
 agent (Foundry) ──project identity, RemoteTool connection──► gateway ──► MCP server (backend.url)
```

The two sides only meet at the gateway: the owner publishes, the consumer reads the **registry**. So a tool can be
used by projects in other repositories, as long as their Foundry projects are in the owner's `shared_with`.

### Publish a tool (owner)

`projects/models-demo/tools/ms-learn/tool.yaml` (Microsoft Learn's public MCP server):

```yaml
name: ms-learn
display_name: Microsoft Learn docs
description: Searches and reads Microsoft Learn documentation (Microsoft's public MCP server).
owner: ai-platform-team
type: mcp                                  # MCP over streamable HTTP
backend:
  url: https://learn.microsoft.com/api/mcp # the MCP endpoint (any path; the gateway endpoint is always .../mcp)
  auth: none                               # or managed_identity + audience: api://<backend app>
tools:                                     # the MCP tools agents may call; any other is refused by the gateway
  - {name: microsoft_docs_search, impact: read}
  - {name: microsoft_docs_fetch, impact: read}
gateway:
  rate_limit_per_minute: 60                # per calling Foundry project
shared_with: []                            # Foundry projects (besides the owner's) whose agents may use it
docs_file: docs.md
tests_file: tests.yaml
```

| Setting | Meaning |
|---|---|
| `backend.auth: none` | public server, or one that authenticates the call some other way; the agent's token never reaches it either way |
| `backend.auth: managed_identity` | the gateway calls the backend with **APIM's managed identity** for `backend.audience`. The owning team grants that identity access on its backend (an app role, or a role on the Azure resource); the pipeline doesn't grant it, and the tool tests show whether it works |
| `tools[].impact` | `read`: no side effects. `write`: changes data, sends something or costs money; agents need approval to call it (below) |
| `shared_with` | the **auth guard**: Foundry project names in this environment's agent account (e.g. `hr-assistant`), or a full Foundry project resource ID for another account. They don't need to be in this repository. The owner's own Foundry project always has access. Removing one takes effect at the next `tool-deploy`: the gateway refuses that project's calls |

`tests.yaml` holds the owner's tests, run by `tool-deploy` through the gateway after every change:

```yaml
- input: Using Microsoft Learn, what is Azure API Management? Give the article URL.
  tool: microsoft_docs_search
  expect: called                           # the tool is called and completes
  must_contain_any: ["learn.microsoft.com"]
- input: Call microsoft_code_sample_search with the query "APIM rate limit policy".
  tool: microsoft_code_sample_search       # on the server, but not exposed in tool.yaml
  expect: refused                          # misuse test: the gateway must refuse it
```

`tool-deploy` creates, per tool:

| Created | Name | Purpose |
|---|---|---|
| gateway API | `tool-<project>-<tool>`, path `tools/<project>/<tool>`, POST/GET/DELETE `/mcp` | MCP streamable HTTP through the gateway, streamed (unbuffered) |
| its policy | `tool-deploy/terraform/policies/tool-api.xml.tftpl` | the tool's values (auth guard, kill switch, exposed tools, rate, backend path and identity) + shared fragments `tool-caller`, `tool-route` (ai-gateway repository) |
| kill switch | `ks-tool-<project>-<tool>` | created `active`; operations suspend the tool for every consumer at once |
| registry entry | `tool-meta-<project>-<tool>` | the contract consumers read: endpoint, exposed tools and impact, `foundry_projects` (owner + `shared_with`), owner |

### Use a tool (consumer, any repository)

1. The owner adds your **Foundry project** to the tool's `shared_with` and merges (`tool-deploy` publishes it).
2. You list the tool in your `project.yaml`; use-case onboarding reads the registry and creates the connection
   (`<project>-<tool>` for your own tool, `<project>-<owner>-<tool>` otherwise). Its plan stops if the tool isn't
   published or isn't shared with your Foundry project.

   ```yaml
   tools:
     use:
       - models-demo/ms-learn                 # <owner project>/<tool>
   ```
3. Your prompt agents list it in `agent.yaml` (`projects/models-demo/agents/docs-assistant`):

   ```yaml
   tools:
     - tool: ms-learn                         # <owner project>/<tool>; a tool of your own project can be just <tool>
       allowed_tools: [microsoft_docs_search, microsoft_docs_fetch]   # default: every exposed tool
       require_approval: never                # default: always when any allowed tool has impact write
   ```

The pipeline turns this into Foundry's MCP tool (server URL `<gateway>/tools/<owner>/<tool>/mcp`,
`project_connection_id` = the connection). `ci` checks that the tool is in `tools.use`; for a tool of this
repository also that `allowed_tools` are exposed and `require_approval: never` is for read tools only. For a tool of
another repository `agent-deploy` checks the same against the gateway registry before it creates the agent version.
Hosted agents call tools from their own code.

### Controls, and where each is enforced

| Control | Enforced by |
|---|---|
| auth guard: only the owner's and `shared_with` Foundry projects (`401 CALLER_NOT_ALLOWED`) | gateway, `tool-caller` (token claim `xms_mirid`) |
| the agent's token never reaches the tool | gateway, `tool-caller` removes it; backend auth is APIM's own identity |
| kill switches `ks-gateway-all`, `ks-tool-<project>-<tool>` (`503 AI_ROUTE_SUSPENDED`) | gateway, `tool-route` |
| calls per minute per calling Foundry project (`429`) | gateway, `tool-route` |
| only exposed MCP tools: the gateway answers `tools/call` of any other tool itself with a tool error `TOOL_NOT_ALLOWED` (MCP result `isError: true`), so the server is never called and the model sees why | gateway, `tool-route` (reads the request only; responses stream through) |
| a consumer connects only to published tools shared with it | use-case onboarding plan (gateway registry) |
| which exposed tools an agent sees, approval for write tools | Foundry, from the agent definition (`allowed_tools`, `require_approval`) |

The gateway checks MCP tool names on each call, so a tool the owner didn't expose can't be called whatever an agent
definition or a model asks for. Each tool has its own gateway API (not APIM's MCP API type, which is in preview):
the gateway passes MCP through as HTTP, which works with any MCP server and keeps every resource in GA APIs.

**Removing a tool:** remove its consumers first (their `tools.use`, then their onboarding removes the connections),
then the tool's folder; its gateway resources are then deleted with `terraform destroy` on its state
(`tool-<project>-<tool>-<env>.tfstate`) by a platform engineer.

## Call an agent through the gateway

The gateway is **transparent**: send exactly the request you would send to Foundry, with
`<gateway URL>/ai/<project>/<agent>/` in place of the agent's Foundry address and the **token(s) the agent's access
list asks for** ([Access to agents](#access-to-agents)). No subscription key. Body, query string (`api-version`,
`agent_session_id`), `traceparent`/`tracestate` and `x-client-*` headers pass through; Foundry's response comes back
unchanged (`x-agent-session-id`, `x-request-id`, …).

| Kind | Foundry, direct | Through the gateway |
|---|---|---|
| hosted | `{project URL}/agents/<agent>/endpoint/protocols/openai/responses?api-version=v1` | `{gateway}/ai/<project>/<agent>/endpoint/protocols/openai/responses?api-version=v1` |
| hosted | `…/agents/<agent>/endpoint/protocols/openai/conversations`, `…/endpoint/sessions[/{id}[:stop]]`, `…/endpoint/protocols/invocations` | the same suffix after `/<agent>/` |
| prompt | `{project URL}/openai/v1/responses` (+ `agent_reference`) | `{gateway}/ai/<project>/<agent>/openai/v1/responses` (the gateway sets `agent_reference`) |
| prompt | `{project URL}/openai/v1/conversations` | `{gateway}/ai/<project>/<agent>/openai/v1/conversations` |

```python
# OpenAI SDK against a hosted agent (access: [gateway]) through the gateway: the same code as against Foundry,
# another base URL and a token for the gateway's agent app
from azure.identity import DefaultAzureCredential
from openai import OpenAI
cred = DefaultAzureCredential()
client = OpenAI(base_url="https://apim-agents-3471ad.azure-api.net/ai/models-demo/basic-code-agent/endpoint/protocols/openai",
                api_key=cred.get_token(f"{AGENTS_APP_ID}/.default").token,   # Authorization: Bearer <agent app token>
                default_query={"api-version": "v1"})
conv = client.conversations.create()
client.responses.create(input="My favourite colour is teal.", conversation=conv.id)
client.responses.create(input="What is my favourite colour?", conversation=conv.id)   # "teal"

# access: [foundry]          → api_key = cred.get_token("https://ai.azure.com/.default").token   (as for Foundry)
# access: [gateway, foundry] → api_key = the agent app token, plus
#                    default_headers={"x-foundry-authorization": "Bearer " + cred.get_token("https://ai.azure.com/.default").token}
```

From a terminal: `az account get-access-token --scope <AGENTS_APP_ID>/.default` for the agent app, `--scope https://ai.azure.com/.default` for Foundry.

What the gateway adds (and the only differences from a direct call):

- **Who:** the agent's access checks ([Access to agents](#access-to-agents)): 401 for a missing or wrong token, 403
  `AGENT_NOT_ALLOWED` without the agent's role. Callers can't set `x-ms-user-identity` or `x-ms-user-isolation-key`.
  Foundry keeps **sessions and conversations per caller** in every mode.
- **Which version:** always the evaluated one. A session you create is bound to it, whatever `version_indicator` says.
- **Where:** only the agent's own endpoints; any other Foundry path gets `404 AGENT_PATH_NOT_ALLOWED`.
- **Limits and switches:** kill switches (503), rate limits and quotas **per caller** (429), content safety on model
  calls.
- **Correlation:** `x-correlation-id` on the response = yours, else your `traceparent` trace ID, else the gateway's.

Conversation history: pass `conversation` (or `previous_response_id`). A session (`agent_session_id`) keeps the
same sandbox and its files, not the chat history.

## Access to agents

Each agent lists in `agent.yaml` **which checks a caller must pass** (`access`, default `[gateway]`). **Every listed
check is required.** The gateway (fragment `agent-caller`) enforces it on every call; anything else is refused before
Foundry sees the call.

| Check | Means |
|---|---|
| `gateway` | a token for the **gateway's agent app** holding the app role `agent.<project>.<agent>` or `agent.<project>.all` |
| `foundry` | the caller's own **Foundry** token; Foundry checks the caller's Foundry role on the project (e.g. **Foundry Agent Consumer**) |

| `access` | Caller sends | Foundry sees | Use it when | Example |
|---|---|---|---|---|
| **`[gateway]`** (default) | `Authorization: Bearer <agent app token>` | APIM's identity (caller's object ID for sessions) | access is managed per agent in Entra; callers need no Foundry role | `basic-code-agent`, proj-default's agents |
| **`[foundry]`** | `Authorization: Bearer <Foundry token>` (as for Foundry) | the caller | callers already have Foundry roles on the project; access per project | `demo-assistant` |
| **`[gateway, foundry]`** | the agent app token in `Authorization` **and** the same identity's Foundry token in `x-foundry-authorization: Bearer …` | the caller (the agent runs as the caller: user-delegated tools) | explicit per-agent permission **and** the agent must act as the caller | `docs-assistant` |

Tokens come from the platform tenant only. Wrong or missing token: 401; agent app token without the role: 403
`AGENT_NOT_ALLOWED`; two tokens of different identities: 401 `CALLER_MISMATCH`; no Foundry role: Foundry's 401/403.
A role change applies with the caller's next token (tokens live about an hour); to stop an agent at once, use its kill
switch.

Listing `foundry` gives callers a Foundry role on the project; until the Foundry account accepts traffic only from
the gateway (private network), such a role also works against Foundry directly, without the gateway's version pinning,
limits and kill switches. Prefer `[gateway]` unless the agent must run as the caller.

### The agent app and its roles

One Entra app per environment, **AI Gateway Agents (`<env>`)** (client ID in `AGENTS_APP_ID`; URI
`api://ai-gateway-agents-<env>`, or `api://<client ID>` where tenant policy requires it, as in dev), set up once by a tenant admin (ai-gateway: `scripts/setup_agents_app.py`). **Assignment required**
is on: an identity without any of its roles gets no token at all.

| Role | Allows | Created by |
|---|---|---|
| `agents.all` | every agent (platform; held by the pipeline identity for smoke tests) | `setup_agents_app.py` |
| `agent.<project>.all` | every agent of the project | an owner of the agent app: `python3 scripts/agent_pipeline.py roles [project ...]` |
| `agent.<project>.<agent>` | that agent (agents whose `access` lists `gateway`) | the same command; it also removes roles of agents that left the project |

Roles can be held by users, groups, apps and managed identities. **Everything in Entra happens outside the
pipelines**: an owner of the agent app creates the roles for the repository's agents (`python3
scripts/agent_pipeline.py roles`, with `az login` as that owner and `AGENTS_APP_ID` set; run it when an agent is
added or removed: agent-deploy's summary lists the roles each project needs), and agent owners with the enterprise
app's owners or a tenant admin decide who holds them. A new agent is callable once its role exists and is assigned;
until then callers can use `agent.<project>.all`.

### Grant access (outside the pipelines)

**Users and groups:** Entra admin center → Enterprise applications → *AI Gateway Agents (`<env>`)* → Users and groups
→ Add user/group → pick the role. (Groups need Entra ID P1.) Or with the CLI:

```bash
APP=<AGENTS_APP_ID>                        # GitHub variable AGENTS_APP_ID of the environment
SP=$(az ad sp show --id $APP --query id -o tsv)
ROLE=$(az ad sp show --id $APP --query "appRoles[?value=='agent.models-demo.basic-code-agent'].id" -o tsv)
WHO=<object ID of the user, group, or service principal>
az rest --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP/appRoleAssignedTo" \
  --body "{\"principalId\":\"$WHO\",\"resourceId\":\"$SP\",\"appRoleId\":\"$ROLE\"}"

# who holds what; remove one with DELETE .../appRoleAssignedTo/<id>
az rest --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP/appRoleAssignedTo" \
  --query "value[].{who:principalDisplayName,role:appRoleId,id:id}" -o table
```

**Foundry role (agents whose `access` lists `foundry`):** an Owner or Role Based Access Control Administrator of the agent Foundry account
assigns **Foundry Agent Consumer** on the project:

```bash
PROJECT_ID=/subscriptions/<FOUNDRY_SUBSCRIPTION_ID>/resourceGroups/<FOUNDRY_RESOURCE_GROUP>/providers/Microsoft.CognitiveServices/accounts/<FOUNDRY_ACCOUNT>/projects/<foundry.project>
az role assignment create --assignee <object ID> --role "Foundry Agent Consumer" --scope "$PROJECT_ID"
```

### Agents calling agents

An agent calls another agent through the gateway like any other caller, with **its own identity**: Foundry gives each
hosted agent an Entra **agent identity**, `<FOUNDRY_ACCOUNT>-<project>-<agent>-AgentIdentity` (checked in dev:
basic-code-agent's tokens carry it, not the project's managed identity). Access is therefore **per calling agent**.
Granting it is done **outside the pipelines**, as above:

1. The called agent's owner agrees; an owner of the agent app assigns the role `agent.<project>.<agent>` of the
   called agent to the **calling agent's identity**. Its object ID: Entra admin center → Enterprise applications →
   search `<project>-<agent>-AgentIdentity`, or `az ad sp list --display-name
   <FOUNDRY_ACCOUNT>-<project>-<agent>-AgentIdentity --query "[].id" -o tsv`, or ask the agent (basic-code-agent's
   tool `gateway_identity`). If the called agent's `access` also lists `foundry`, the calling identity also needs the
   Foundry role on the called project (and sends both tokens).
2. The calling hosted agent gets a token with `AI_GATEWAY_AGENTS_SCOPE` (set by the pipeline) and calls
   `AI_GATEWAY_URL`:

```python
import os, requests
from azure.identity import DefaultAzureCredential
token = DefaultAzureCredential().get_token(os.environ["AI_GATEWAY_AGENTS_SCOPE"]).token
r = requests.post(f"{os.environ['AI_GATEWAY_URL']}/ai/proj-default/leave-policy-agent/openai/v1/responses",
                  headers={"Authorization": f"Bearer {token}"}, json={"input": "How many days of annual leave?"})
```

Example: `basic-code-agent` asks `proj-default/leave-policy-agent` (tool `ask_agent`, `CALLABLE_AGENTS` in its
`env/dev.env`); the Postman demo walks through it ([postman/README.md](postman/README.md)).

The called agent's model calls count against **its** project's model quota; its rate limits and quotas count per
calling identity. The called agent sees the calling agent, not the user behind it.

## Add an agent

1. Copy an agent folder (prompt: `projects/proj-default/agents/leave-policy-agent`; code:
   `projects/models-demo/agents/basic-code-agent`) to `projects/<project>/agents/<new-agent>` (lowercase, digits,
   hyphens), and set its `model` and `judge_model` to the project's `<connection>/<model>`.
2. Edit `agent.yaml` (name must match the folder), the instructions or code, docs, evaluation dataset and smoke tests.
3. Open a pull request; `ci` and the project's plan check it. Merge to deploy (stage dev).

## Add a project (use case)

A new use case is a new folder `projects/<project>/`. One merge creates its Foundry project, connects its models and
deploys its agents, in that order, with no workflow to add:

```
use-case-onboarding (project.yaml is new)          agent-deploy (waits for this commit's onboarding run)
  dev / onboard <project>                           dev / release agents <project>
    · Foundry project in the environment's            · a Foundry version per agent
      agent account                                   · evaluation gate
    · model API models/<project> (limits,             · agent API ai/<project> on the gateway
      content safety)                                 · Developer Portal, smoke tests
    · allocation model-limit-<project>-<model>
    · AI Gateway connection <models.connection>
    · smoke test: an agent answers via each model
```

Tools under `projects/<project>/tools/` are published first by tool-deploy (onboarding waits for it).

### Use-case template

Create these files (replace `<project>`, `<agent>`, `<team>` and the TODOs). Names: lowercase letters, digits and
hyphens; the project name must be unique across every repository that deploys to the same platform, because it names
the Foundry project, the gateway API, the connections and the Terraform state.

```
projects/<project>/
  project.yaml
  project.md                          the project's page in the Developer Portal
  agents/<agent>/
    agent.yaml
    instructions.md                   what the agent does (prompt agent)
    docs.md                           the agent's page in the Developer Portal
    tests.yaml
    evals.yaml
```

`projects/<project>/project.yaml`

```yaml
name: <project>                          # = folder name
display_name: TODO
owner: <team>
foundry:
  project: <project>                     # created by use-case onboarding in the environment's agent account
gateway:
  path: ai/<project>                     # https://<apim>.azure-api.net/ai/<project>/<agent>/...
  product: agents-<project>              # created if missing; groups the project's agents in the Developer Portal
  rate_limit_per_minute: 60              # per caller, across the project's agents
  quota_per_day: 1000
docs_file: project.md
models:
  connection: <project>-models           # unique in the agent account (platform-models, proj-default-models are taken)
  # content_safety: {violence: 2}        # optional: stricter than the platform default (4)
  allowed:
    - name: gpt-5.4-mini                 # registered on the gateway (ai-gateway: model-platform-deployment)
      tokens_per_minute: 10000           # this project's share; must fit the model's free capacity
      tokens_per_day: 200000
# tools:
#   use: [<owner project>/<tool>]        # optional: tools published and shared with this project
```

`projects/<project>/agents/<agent>/agent.yaml`

```yaml
name: <agent>                            # = folder name
display_name: TODO
description: TODO one sentence on what the agent answers
owner: <team>
model: <project>-models/gpt-5.4-mini     # <connection>/<model>: models only through the gateway
instructions_file: instructions.md
access: [gateway]                        # checks a caller must pass, all required: [gateway] (default) | [foundry] | [gateway, foundry]
gateway:
  rate_limit_per_minute: 30              # per caller, for this agent
  quota_per_day: 500
docs_file: docs.md
tests_file: tests.yaml
evals_file: evals.yaml
```

`projects/<project>/agents/<agent>/tests.yaml` and `evals.yaml`

```yaml
# tests.yaml: smoke tests through the gateway after each release
- input: "Reply with the single word OK."
  must_contain_any: ["OK"]
```

```yaml
# evals.yaml: every evaluator must reach min_pass_rate, or the gateway keeps the previous version
judge_model: <project>-models/gpt-5.4-mini
min_pass_rate: 0.8
evaluators: [relevance, coherence, intent_resolution]
dataset:                                 # at least 3 representative questions
  - query: TODO
  - query: TODO
  - query: TODO
```

For a **hosted** (container) agent, start from `projects/models-demo/agents/basic-code-agent/` (`kind: hosted`,
`container:`, `src/` with the code, `Dockerfile` and `.dockerignore`) and ask the platform team to grant the new
project's managed identity **AcrPull** on the environment's container registry before the first merge: the pipeline
can't grant roles, and without it the agent version fails to start (the pipeline stops before the gateway changes).
Details: [Hosted agents as container images (ACR)](#hosted-agents-as-container-images-acr). Prompt agents need
nothing extra.

### Where a new project can stop

All of these are caught before anything changes in Azure:

| Check | Caught by |
|---|---|
| names match folders; required files; at least one smoke test; an evaluation with at least 3 queries | `ci` on the pull request |
| the model connection name is already used by another project | `ci` and the onboarding plan |
| the Foundry project name is already used by another project | `ci` and the onboarding plan |
| the model isn't registered, or not enough free capacity | onboarding plan on the pull request |
| a tool in `tools.use` isn't published or isn't shared with this project | onboarding plan |

Callers need no subscription key: access is the agent's `access` list ([Access to agents](#access-to-agents)); give
callers the role `agent.<project>.all` for every agent of a project, or one agent's role.

## Use cases in another repository

A use-case team can keep its projects in its own repository instead of this one. Everything a project needs from the
platform is on the platform itself (the gateway registry, the shared policy fragments, the environment's Foundry
account), so another repository works the same way, but **it needs its own pipeline setup**:

| Set up in the other repository | How |
|---|---|
| The pipelines | copy `.github/` (workflows `tool-deploy`, `use-case-onboarding`, `agent-deploy`, `ci`, the units `_tool`, `_project`, `_agents`, the actions), `scripts/`, `onboarding/`, `tool-deploy/` and `templates/` from this repository; its projects go under `projects/` (use-case template above). Keep the copy in step with this repository's releases |
| GitHub environments | `model-dev`, `agent-dev` (and `-uat`, `-prd`, each with required reviewers): `scripts/set_github_environment.sh <env> KEY=VALUE ...` with the **same** platform coordinates as here ([variables](#environment-settings-github-variables)) |
| Azure sign-in | the platform team adds a federated credential per GitHub environment of the new repository on the pipeline identity (subject `repo:<owner>/<repository>:environment:<model\|agent>-<env>`), or gives the team its own identity with the same [permissions](#permissions); then `AZURE_CLIENT_ID` on each GitHub environment |
| Hosted agents | `ACR_NAME` on its `agent-<env>` environments (the platform's registry for that environment), and AcrPull on that registry for each of its projects' identities (platform team) |

Rules across repositories:
- **Unique names.** Project names, Foundry project names and model connection names must be unique across every
  repository on the platform: the offline checks only see their own repository, and the gateway, Foundry and the
  Terraform state are shared. Prefix them with the team (for example `hr-leave-assistant`).
- **Model capacity** is shared: the onboarding plan reads every project's allocation from the gateway, whichever
  repository it came from.
- **Tools** from another repository are used through the gateway registry (`tools.use: [<owner>/<tool>]`), once the
  owner shares them with your Foundry project ([Tools](#tools-project-owned-mcp-servers)).
- **Order across repositories** is not automatic: a project that uses another repository's tool can only be
  onboarded once that tool is published (the onboarding plan stops with a clear message until then).

## Version served and stopping an agent

- **Version served:** the named value `agent-version-<agent>`. Only the pipeline moves it, and only to a version
  that passed evaluation in the same run. To roll back, set it to an earlier version (or revert in Git and merge).
  Hosted agents: the agent endpoint's version selector in Foundry serves it (set by the same step); roll back by
  reverting in Git and merging, so both move together.
- **Stop an agent:** set its kill switch `ks-agent-<agent>` to `suspended` (APIM, Named values); callers get
  `503 AI_ROUTE_SUSPENDED`. The pipeline creates it once and never resets it; the smoke test skips suspended agents.
  The model and global kill switches are the gateway repository's.

## Environments

| Environment | Resources (today) | GitHub environments | Approval | Projects |
|---|---|---|---|---|
| **dev** | subscription `scbx-testing`: APIM `apim-agents-3471ad`, agent Foundry account `az-aif-tst-model-npr-01` (models in the gateway's models-only account `aif-tst-models-3471ad`) | `model-dev`, `agent-dev` | none | models-demo, proj-default |
| **uat** | the same as dev until uat resources exist | `model-uat`, `agent-uat` | required reviewers | none yet |
| **prd** | the same as dev until prd resources exist | `model-prd`, `agent-prd` | required reviewers | none yet |

Pipeline identity: `id-promptops-pipeline`, with a federated credential per GitHub environment
(`repo:<owner>/agent-deployment:environment:<model|agent>-<env>`). In the gateway repository this platform is its
`test` environment.

**Give uat or prd their own resources:**
1. The platform team creates them (the gateway repository's platform stack and pipelines).
2. Create a pipeline identity for the environment with a federated credential for each of its GitHub environments
   and the [permissions](#permissions).
3. `scripts/set_github_environment.sh <env> KEY=VALUE ...` with the new coordinates and `AZURE_CLIENT_ID`.
4. Add the environment to the repository variable `PLATFORM_ENVIRONMENTS` (e.g. `dev,uat,prd`); the next release tag
   promotes every project that doesn't restrict it.

The gateway repository's sandbox (`gateway/sandbox`) is a separate, disposable copy of the platform for trying
platform changes; no project deploys to it.

## Run locally

```bash
pip install -r scripts/requirements.txt
az login
# The environment's variables, as the pipeline gets them (agent-<env> holds the same set)
eval "$(gh variable list --env model-dev --json name,value -q '.[] | "export \(.name)=\(.value)"')"
for v in APIM_SUBSCRIPTION_ID APIM_RESOURCE_GROUP APIM_NAME MODELS_PRODUCT FOUNDRY_SUBSCRIPTION_ID FOUNDRY_RESOURCE_GROUP FOUNDRY_ACCOUNT; do
  export TF_VAR_$(echo $v | tr A-Z a-z)="${!v}"; done

python scripts/agent_pipeline.py validate           # offline checks of every definition
python scripts/pipeline_plan.py agents --base HEAD~1   # what agent-deploy would deploy for this commit (also: tools, onboarding)

# onboarding plan (tool-deploy/terraform works the same way, state tool-<project>-<tool>-<env>.tfstate)
cd onboarding/terraform
terraform test                                      # mocked plan tests, no Azure access
terraform init -backend-config="resource_group_name=$TF_STATE_RESOURCE_GROUP" -backend-config="storage_account_name=$TF_STATE_STORAGE_ACCOUNT" \
  -backend-config="container_name=$TF_STATE_CONTAINER" -backend-config="key=ai-gateway-model-access-proj-default-dev.tfstate" \
  -backend-config="use_azuread_auth=true"
terraform plan -var environment=dev -var project=proj-default
cd ../..
python scripts/model_smoke.py access proj-default
python scripts/tool_smoke.py models-demo ms-learn

# agents
python scripts/agent_pipeline.py deploy   proj-default
python scripts/agent_pipeline.py evaluate proj-default
python scripts/agent_pipeline.py register proj-default
python scripts/agent_pipeline.py smoke    proj-default
```
