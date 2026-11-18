locals {
  repo_root = "${path.module}/../.."
  # The environment's coordinates, from the platform variables (GitHub environment variables).
  env = {
    gateway        = { subscription_id = var.apim_subscription_id, resource_group = var.apim_resource_group, apim_name = var.apim_name, models_product = var.models_product }
    agents_foundry = { subscription_id = var.foundry_subscription_id, resource_group = var.foundry_resource_group, account_name = var.foundry_account }
  }
  projects_root = var.projects_dir != "" ? "${path.module}/${var.projects_dir}" : "${local.repo_root}/projects"
  project       = yamldecode(file("${local.projects_root}/${var.project}/project.yaml"))
  t             = yamldecode(file("${local.projects_root}/${var.project}/tools/${var.tool}/tool.yaml"))

  # The owner's Foundry project (project.yaml foundry.project, default: the folder name).
  owner_foundry_project = try(local.project.foundry.project, var.project)

  # Auth guard: the Foundry projects whose agents may call the tool. The owner's own, plus shared_with: Foundry project
  # names in this environment's agent account, or full project resource IDs (/subscriptions/...) for another account.
  # They need not be in this repository. The gateway compares them with the caller token's xms_mirid (lowercase).
  foundry      = local.env.agents_foundry
  mirid_prefix = lower("/subscriptions/${local.foundry.subscription_id}/resourcegroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}/projects/")
  shared_with  = [for s in try(local.t.shared_with, []) : tostring(s)]
  callers = distinct([for p in concat([local.owner_foundry_project], local.shared_with) :
  startswith(p, "/subscriptions/") ? lower(p) : "${local.mirid_prefix}${p}"])

  # The gateway endpoint is always .../mcp; the backend path is the last segment of backend.url.
  backend_url   = try(tostring(local.t.backend.url), "")
  url_parts     = try(regex("^(https://.+)/([^/?#]+)$", local.backend_url), ["", ""])
  backend_base  = local.url_parts[0]
  backend_leaf  = local.url_parts[1]
  backend_auth  = try(local.t.backend.auth, "none")
  audience      = try(local.t.backend.audience, "")
  tool_names    = [for x in try(local.t.tools, []) : try(x.name, "")]
  rate          = try(local.t.gateway.rate_limit_per_minute, 0)
  api_name      = "tool-${var.project}-${var.tool}"
  path          = "tools/${var.project}/${var.tool}"
  kill_switch   = "ks-tool-${var.project}-${var.tool}"
  endpoint      = "${data.azurerm_api_management.this.gateway_url}/${local.path}/mcp"
  fragments     = distinct(flatten(regexall("fragment-id=\"([^\"]+)\"", file("${path.module}/policies/tool-api.xml.tftpl"))))
  deployed      = [for f in try(data.azapi_resource_list.policy_fragments.output.value, []) : f.name]
  project_envs  = try(local.project.environments, ["dev"])
  foundry_names = [for s in local.shared_with : s if !startswith(s, "/subscriptions/")]
}
