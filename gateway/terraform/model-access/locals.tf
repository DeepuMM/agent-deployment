locals {
  repo_root = "${path.module}/../../.."
  env       = yamldecode(file("${local.repo_root}/platform/environments/${var.environment}.yaml"))
  project   = yamldecode(file("${local.repo_root}/projects/${var.project}/project.yaml"))

  connection = try(local.project.models.connection, "platform-models")
  allowed    = { for m in try(local.project.models.allowed, []) : m.name => m }

  foundry    = local.env.agents_foundry
  project_id = "/subscriptions/${local.foundry.subscription_id}/resourceGroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}/projects/${var.project}"
  # Token claim xms_mirid carries the calling project's resource ID in this exact form (lowercase resourcegroups).
  project_mirid = "/subscriptions/${local.foundry.subscription_id}/resourcegroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}/projects/${var.project}"

  # Named values on the gateway, read through ARM.
  named_values = { for nv in try(data.azapi_resource_list.named_values.output.value, []) : nv.name => nv.properties }

  # Model registry: written only by the model onboarding stack (model-meta-<model>).
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
