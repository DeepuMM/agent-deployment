locals {
  repo_root = "${path.module}/../.."
  # The environment's coordinates, from the platform variables (GitHub environment variables).
  env = {
    gateway        = { subscription_id = var.apim_subscription_id, resource_group = var.apim_resource_group, apim_name = var.apim_name, models_product = var.models_product }
    agents_foundry = { subscription_id = var.foundry_subscription_id, resource_group = var.foundry_resource_group, account_name = var.foundry_account }
  }
  projects_root = var.projects_dir != "" ? "${path.module}/${var.projects_dir}" : "${local.repo_root}/projects"
  project       = yamldecode(file("${local.projects_root}/${var.project}/project.yaml"))

  # The Foundry project this repo project's agents run in: foundry.project in project.yaml (default: the folder name),
  # in the environment's agent Foundry account. Onboarding creates it, or adopts one made earlier (foundry.existing).
  # The repo project (folder name) names the gateway objects: model API models-<project>, allocations model-limit-*.
  foundry_project = try(local.project.foundry.project, var.project)
  # Environments the project deploys to, in promotion order (project.yaml environments, default [test]).
  project_environments = try(local.project.environments, ["test"])
  foundry_existing     = try(local.project.foundry.existing, false)

  # Other repo projects that name the same Foundry project. Each repo project has its own.
  other_project_owners = [
    for f in fileset(local.projects_root, "*/project.yaml") : dirname(f)
    if dirname(f) != var.project && try(yamldecode(file("${local.projects_root}/${f}")).foundry.project, dirname(f)) == local.foundry_project
  ]

  connection = try(local.project.models.connection, "platform-models")

  # Other repo projects in this environment with the same connection name: Foundry connection names are unique per
  # agent account, so a second one is refused by Foundry ("can only be updated by the workspace that created it").
  other_connection_users = [
    for f in fileset(local.projects_root, "*/project.yaml") : dirname(f)
    if dirname(f) != var.project
    && contains(try(yamldecode(file("${local.projects_root}/${f}")).environments, ["test"]), var.environment)
    && try(yamldecode(file("${local.projects_root}/${f}")).models.connection, "platform-models") == local.connection
  ]
  allowed = { for m in try(local.project.models.allowed, []) : m.name => m }

  foundry          = local.env.agents_foundry
  account_id       = "/subscriptions/${local.foundry.subscription_id}/resourceGroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}"
  project_id       = "${local.account_id}/projects/${local.foundry_project}"
  project_endpoint = "https://${local.foundry.account_name}.services.ai.azure.com/api/projects/${local.foundry_project}"
  # Token claim xms_mirid carries the calling project's resource ID in this exact form (lowercase resourcegroups).
  project_mirid = "/subscriptions/${local.foundry.subscription_id}/resourcegroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}/projects/${local.foundry_project}"

  # Named values on the gateway, read through ARM.
  # Fragments the model API policy includes, and which of them are deployed on the gateway.
  needed_fragments   = distinct(flatten(regexall("fragment-id=\"([^\"]+)\"", file("${path.module}/policies/project-models-api.xml.tftpl"))))
  deployed_fragments = [for f in try(data.azapi_resource_list.policy_fragments.output.value, []) : f.name]
  named_values       = { for nv in try(data.azapi_resource_list.named_values.output.value, []) : nv.name => nv.properties }

  # Model registry: written only by the model platform deployment stack (model-meta-<model>).
  registry = {
    for name, p in local.named_values :
    jsondecode(p.value).name => jsondecode(p.value)
    if startswith(name, "model-meta-")
  }

  # Tokens per minute already given to OTHER projects, per model (named values model-limit-<project>-<key>).
  others = [
    for name, p in local.named_values : jsondecode(p.value)
    if startswith(name, "model-limit-") && try(jsondecode(p.value).project, "") != var.project
  ]
  allocated_by_others = {
    for m in keys(local.allowed) : m => sum(concat([0], [for o in local.others : o.tokens_per_minute if o.model == m]))
  }

  # Content safety (shared fragment model-content-safety), resolved per model: the model's own setting
  # (models.allowed[].content_safety), else the project's (models.content_safety), else the platform default, which
  # lives in the fragment (severity 4 per category on a 0-7 scale, lower is stricter; Prompt Shields on). Settings can
  # only be stricter. A model's routing-table branch sets a variable only for what the model or project sets.
  # check_answers: opt-in answer checks (enforce-on-completions), off by default; true or false at either level.
  content_safety_defaults = { hate = 4, sexual = 4, self_harm = 4, violence = 4, prompt_shields = true, check_answers = false }
  # Values as strings (numbers and true/false mix in one mapping); the policy renders them as @(<value>).
  content_safety_project = try({ for k, v in local.project.models.content_safety : k => tostring(v) }, {})
  content_safety_models = {
    for name, m in local.allowed : name => try({ for k, v in m.content_safety : k => tostring(v) }, {})
  }
  # Every (scope, settings) pair, for validation.
  content_safety_scopes = merge(
    { "models.content_safety" = local.content_safety_project },
    { for name, cs in local.content_safety_models : "models.allowed[${name}].content_safety" => cs },
  )

  # Everything the policy needs per allowed model; registry values only, nothing typed in the project.
  models = {
    for name, m in local.allowed : name => {
      name              = name
      backend_id        = try(local.registry[name].backend_id, "")
      deployment        = try(local.registry[name].deployment, "")
      kill_switch       = try(local.registry[name].kill_switch, "")
      model             = try(local.registry[name].model, "")
      version           = try(local.registry[name].version, "")
      format            = try(local.registry[name].format, "OpenAI")
      tokens_per_minute = m.tokens_per_minute
      tokens_per_day    = m.tokens_per_day
      content_safety = [
        for k, v in merge(local.content_safety_project, local.content_safety_models[name]) : {
          name  = "cs-${replace(k, "_", "-")}"
          value = tostring(v)
        } if contains(keys(local.content_safety_defaults), k)
      ]
    }
  }

  # Developer Portal page for the project's model API.
  description = join("\n", concat([
    "Platform models for project **${var.project}**, served by the AI gateway.",
    "",
    "Agents in the project use them as `${local.connection}/<model>`. Foundry calls this API with the project's managed identity; no subscription key is used and no other caller is accepted.",
    "",
    "| Model | Use in an agent | Tokens per minute | Tokens per day | Region | Content safety |",
    "|---|---|---|---|---|---|",
    ], [for n, m in local.models : "| **${n}**: ${try(local.registry[n].summary, "")} | `${local.connection}/${n}` | ${m.tokens_per_minute} | ${m.tokens_per_day} | ${try(local.registry[n].region, "")} | ${try(local.registry[n].guardrail, "")} |"], [
    "",
    "**Responses:** `401` caller is not this project · `404 MODEL_NOT_ALLOWED` model not in the project's list · `429` token limit reached (see `x-ratelimit-remaining-tokens`) · `503 AI_ROUTE_SUSPENDED` model suspended by the platform kill switch.",
  ]))

  openapi = {
    openapi = "3.0.1"
    info    = { title = "Models - ${var.project}", version = "1.0", description = local.description }
    servers = [{ url = "${data.azurerm_api_management.this.gateway_url}/models/${var.project}" }]
    paths = {
      "/deployments/{deployment-id}/chat/completions" = {
        post = {
          operationId = "chat-completions"
          summary     = "Chat completions"
          description = "Chat completions with a platform model. `deployment-id` is the gateway model name: ${join(", ", [for n in keys(local.models) : "`${n}`"])}."
          parameters = [
            { name = "deployment-id", in = "path", required = true, description = "Gateway model name", schema = { type = "string", enum = keys(local.models) } },
            { name = "api-version", in = "query", required = false, schema = { type = "string", default = var.inference_api_version } },
          ]
          requestBody = {
            required = true
            content = { "application/json" = {
              schema = { type = "object", required = ["messages"], properties = {
                messages   = { type = "array", items = { type = "object", properties = { role = { type = "string" }, content = { type = "string" } } } }
                max_tokens = { type = "integer" }
              } }
              example = { messages = [{ role = "user", content = "Reply with the single word OK." }] }
            } }
          }
          responses = {
            "200" = { description = "Chat completion (Azure OpenAI format)" }
            "401" = { description = "Caller is not this project's Foundry identity" }
            "404" = { description = "MODEL_NOT_ALLOWED: model not in the project's list" }
            "429" = { description = "Token limit or daily quota reached" }
            "503" = { description = "AI_ROUTE_SUSPENDED: model suspended by the kill switch" }
          }
        }
      }
    }
  }
}
