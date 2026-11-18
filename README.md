# Agent deployment (project repository)

Onboards use cases onto the AI platform and deploys their agents. Each project gets **its own Microsoft Foundry
project** and governed access to platform models (use-case onboarding); then its agents are deployed, gated by a
Foundry evaluation, and published on the AI gateway (Azure API Management) with documentation in the Developer Portal.

The platform itself lives in the gateway repository, **[ai-gateway](https://github.com/DeepuMM/ai-gateway)**: the
gateway, model hosting, and the shared gateway policy that every project uses (kill switches, content safety,
routing, limits). See its README for the APIM structure, the policy fragments and how the kill switches work.

## Pipelines

A release is made of **stages**, one per environment. A stage (`_stage.yml`) runs three **units** for one project
in one environment, each deploying one thing: tools (`_tool`) ─► onboarding (`_project`) ─► agents (`_agents`).
Triggers are thin: each project has its own, plus one for the whole repository. All sign in to Azure with GitHub
OIDC (no secrets).

```
.github/workflows/
  project-<project>.yml   trigger, one per project (GENERATED)      detect ─► dev
  release-all.yml         trigger, every project (tags, shared code) detect ─► dev ─► uat ─► prd   (via _release.yml)
  ci.yml                  repository checks (no Azure)

  _detect.yml             what changed (scripts/release_plan.py)
  _stage.yml              one stage: _tool (per tool) ─► _project ─► _agents
  _tool.yml, _project.yml, _agents.yml   the units
.github/actions/setup, tf-init   shared steps (Python, Terraform, Azure sign-in, state backend)
```

A project's run shows two boxes, `detect` and its stage; click the stage to see the units. Its trigger is generated
with only the project's first environment (dev): merges never promote. Promotion is a release tag, through
`release-all` (`detect ─► dev ─► uat ─► prd`, a stage skipped when the project doesn't list it).

### Triggers

| Event | Workflow | Runs |
|---|---|---|
| pull request touching `projects/<project>/` | `project-<project>.yml` | plans and validation for that project, in dev (nothing changes) |
| merge to `main` touching `projects/<project>/` | `project-<project>.yml` | stage **dev** for that project, only the units with something to do |
| manual run of a project | `project-<project>.yml` | stage dev, every unit of the project |
| pull request / merge touching shared code (`onboarding/`, `tool-deploy/`, `templates/`) | `release-all.yml` | plans (PR) or stage dev (merge) for **every** project |
| release tag `v*` | `release-all.yml` | every project: stage **dev**, then **uat** and **prd** (each after approval) for projects that list them |
| any pull request or merge | `ci.yml` | trigger files up to date, validation of every definition, hosted agents compile, Terraform plan tests (mocked) |

`release-all` runs projects one at a time, those that own tools first, so tools are published before other projects
connect to them.

### Units

Which units run is decided per project by `scripts/release_plan.py`: a change under `tools/<tool>/` publishes that
tool; a change to `project.yaml` re-onboards; any other change (agents, `project.md`) releases the agents. Manual runs,
release tags and shared-code changes run everything. A unit runs only if the previous ones succeeded or had nothing
to do.

| Unit | GitHub environment | What it does | Terraform state |
|---|---|---|---|
| `_tool.yml` (per tool) | `model-<env>` | publishes the tool on the gateway (API `tools/<project>/<tool>/mcp`, auth guard from `shared_with`, kill switch, registry entry), then the tool tests: another identity refused, the owner's tests and misuse tests. See [Tools](#tools-project-owned-mcp-servers) | `tool-<project>-<tool>-<env>.tfstate` |
| `_project.yml` | `model-<env>` | **use-case onboarding**: the Foundry project, its model API (limits, content safety), its allocation, the AI Gateway connection, a connection per tool in `tools.use`; then a temporary agent answers through each model, another identity is refused, and the Developer Portal is published | `ai-gateway-model-access-<project>-<env>.tfstate` |
| `_agents.yml` | `agent-<env>` | validate; new Foundry versions for changed agents; **evaluation gate** (every evaluator at `min_pass_rate`, else the gateway keeps the previous version); register the agent API `ai/<project>` pinned to the evaluated versions, kill switches, product; Developer Portal; smoke tests through the gateway | none (APIM and Foundry are the record) |

**When a run stops, nothing has changed yet** if it stops in a plan: a model isn't registered or has no capacity left,
a tool isn't published or isn't shared with the project, a shared fragment is missing on the gateway, names collide,
content safety is looser than the platform, or a definition is invalid. Later failures (Azure refuses a change, an
evaluation or a test fails) leave the previous agent versions served.

### Stages and promotion

Three environments, in promotion order: **dev → uat → prd**. A project lists the ones it deploys to:

```yaml
environments: [dev]                        # default [dev]; later [dev, uat, prd]
```

| Stage | Runs on | Approval | GitHub environments |
|---|---|---|---|
| **dev** | every merge to `main`, manual runs, release tags | none | `model-dev`, `agent-dev` |
| **uat** | release tags `v*`, after the project's dev stage for that commit succeeded | required reviewers | `model-uat`, `agent-uat` |
| **prd** | release tags `v*`, after uat succeeded | required reviewers | `model-prd`, `agent-prd` |

- A tag promotes **exactly the tagged commit**: dev, then uat after approval, then prd after approval. Every unit of a
  stage must succeed (or have nothing to do) before the next stage starts.
- Approval is GitHub's environment protection: each unit job of uat and prd waits until a reviewer approves it in the
  run (Actions > the run > Review deployments).
- Each environment has its own gateway, registry, kill switches, Foundry account and Terraform state, so a stage
  can't affect another. Agents are evaluated again in each environment (Foundry versions belong to an account), and
  tools are published in an environment before projects there connect to them.
- **Today** every project lists only `dev`. uat and prd exist as GitHub environments (with approval), with the same
  coordinates as dev until their own resources exist; add them to a project's `environments` once they do.

### Environment settings: GitHub variables

Nothing about an environment is kept in this repository. Each GitHub environment holds its coordinates as
**variables** (not secrets); the units pass them to Terraform (`TF_VAR_*`) and to the scripts
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
| **API Management Service Contributor** | the APIM instance | model, tool and agent APIs, named values, products, portal, smoke-test subscription |
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
| **Use-case onboarding** | — | `_project` unit: the use case's **Foundry project**, its model API, token budget, content safety settings and **connected models** |
| **Agent deployment** | — | `_agents` unit: agent versions, evaluation gate, gateway publishing, docs, smoke test |

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
| `projects/<project>/agents/<agent>/docs.md` | operation documentation in the Developer Portal |
| `projects/<project>/agents/<agent>/evals.yaml` | evaluation gate: judge model, minimum pass rate, evaluators, dataset |
| `projects/<project>/agents/<agent>/tests.yaml` | smoke tests (`input`, `must_contain_any`) |
| `projects/<project>/tools/<tool>/tool.yaml` | a tool the project owns (MCP server): backend, exposed tools and their impact, rate, `shared_with`; see [Tools](#tools-project-owned-mcp-servers) |
| `projects/<project>/tools/<tool>/docs.md`, `tests.yaml` | the tool's page for agent builders; the owner's tests (`expect: called` and misuse tests `expect: refused`) |
| `onboarding/terraform` | use-case onboarding stack (state `ai-gateway-model-access-<project>-<env>.tfstate`) |
| `templates/` | the agent operation and project API policies: each agent's and project's own values, plus the shared fragments by name |
| `scripts/agent_pipeline.py` | `validate`, `deploy`, `evaluate`, `register`, `smoke` |
| `scripts/model_smoke.py` | onboarding smoke test (`access`) |
| `scripts/tool_smoke.py` | tool tests for the `_tool` unit (auth guard, owner's tests, misuse tests) |
| `tool-deploy/terraform` | tool deployment stack (state `tool-<project>-<tool>-<env>.tfstate`) |
| `.github/workflows/` | `project-<project>.yml` (generated triggers), `release-all.yml`, `ci.yml`, and the shared `_detect`, `_stage`, `_release`, `_tool`, `_project`, `_agents` |
| `.github/actions/` | shared steps: `setup` (Python, Terraform, Azure sign-in), `tf-init` (state backend) |
| `scripts/release_plan.py` | which units a project release runs |
| `scripts/scaffold_workflows.py` | writes the per-project trigger workflows (`--check` in CI) |
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
environments: [dev]              # where it deploys, in promotion order (dev, uat, prd)
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

The container listens on port 8088 (the agent server's default; see the sample's `Dockerfile`). Instead of
`container:`, a hosted agent can use `code:` (`source_dir`, `runtime` python_3_11/12/13, `entry_point`,
`dependency_resolution: remote_build`): the pipeline zips the folder and Foundry builds it, with no registry.

**Container registry, per environment:** one Azure Container Registry for all hosted agents of the environment
(dev: `acrscbx3471ad`, GitHub variable `ACR_NAME` on `agent-<env>`). Each image is
`<registry>.azurecr.io/<project>/<agent>:<first 16 characters of the source's SHA-256>`, and the agent version's
metadata records it. The pipeline identity builds and pushes (it has Contributor on the registry's resource group;
the registry uses the classic permission mode). Foundry pulls the image with the **project's managed identity**, which
needs **AcrPull** on the registry: granted per project by the platform team when a project starts using hosted agents
(without it the version fails to start and the pipeline stops before the gateway changes).

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
 projects/<owner>/tools/<tool>/ ──_tool unit──► tools/<owner>/<tool>/mcp          project.yaml tools.use: [<owner>/<tool>]
                                           │   auth guard: owner + shared_with        │ onboarding (_project) reads the
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

`tests.yaml` holds the owner's tests, run by the `_tool` unit through the gateway after every change:

```yaml
- input: Using Microsoft Learn, what is Azure API Management? Give the article URL.
  tool: microsoft_docs_search
  expect: called                           # the tool is called and completes
  must_contain_any: ["learn.microsoft.com"]
- input: Call microsoft_code_sample_search with the query "APIM rate limit policy".
  tool: microsoft_code_sample_search       # on the server, but not exposed in tool.yaml
  expect: refused                          # misuse test: the gateway must refuse it
```

The `_tool` unit creates, per tool:

| Created | Name | Purpose |
|---|---|---|
| gateway API | `tool-<project>-<tool>`, path `tools/<project>/<tool>`, POST/GET/DELETE `/mcp` | MCP streamable HTTP through the gateway, streamed (unbuffered) |
| its policy | `tool-deploy/terraform/policies/tool-api.xml.tftpl` | the tool's values (auth guard, kill switch, exposed tools, rate, backend path and identity) + shared fragments `tool-caller`, `tool-route` (ai-gateway repository) |
| kill switch | `ks-tool-<project>-<tool>` | created `active`; operations suspend the tool for every consumer at once |
| registry entry | `tool-meta-<project>-<tool>` | the contract consumers read: endpoint, exposed tools and impact, `foundry_projects` (owner + `shared_with`), owner |

### Use a tool (consumer, any repository)

1. The owner adds your **Foundry project** to the tool's `shared_with` and merges (the `_tool` unit publishes it).
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
another repository the `_agents` unit checks the same against the gateway registry before it creates the agent version.
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
3. Open a pull request; `ci` and the project's plan check it. Merge to deploy (stage dev).

## Add a project

Create `projects/<project>/project.yaml` (as above: `environments`, `foundry.project`, `gateway.path` = `ai/<project>`,
`models` with a connection name not used by another project in the environment) and `project.md`, then add agents,
and run `python scripts/scaffold_workflows.py` to create its trigger workflow (CI fails without it). Merge: onboarding
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
4. Add the environment to `environments` in each project that should go there; the next release tag promotes them.

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
python scripts/release_plan.py proj-default --base HEAD~1   # what a release of this commit would run

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
