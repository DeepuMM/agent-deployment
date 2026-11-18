## Validation (fails the plan before anything changes)

resource "terraform_data" "validated" {
  input = local.api_name

  lifecycle {
    precondition {
      condition     = contains(local.project_envs, var.environment)
      error_message = "projects/${var.project}/project.yaml environments (${join(", ", local.project_envs)}) doesn't list ${var.environment}."
    }
    precondition {
      condition     = length(setsubtract(local.fragments, local.deployed)) == 0
      error_message = "Shared policy fragment(s) for tools missing on the gateway: ${join(", ", setsubtract(local.fragments, local.deployed))}. Run the ai-gateway repository's gateway-policy-deployment workflow for this environment first."
    }
    precondition {
      condition     = try(local.t.name, "") == var.tool && can(regex("^[a-z0-9][a-z0-9-]{1,40}$", var.tool)) && try(local.t.type, "") == "mcp"
      error_message = "tool.yaml: name must match the folder (lowercase letters, digits, hyphens) and type must be mcp."
    }
    precondition {
      condition     = local.backend_base != "" && contains(["none", "managed_identity"], local.backend_auth) && (local.backend_auth != "managed_identity" || startswith(local.audience, "api://") || startswith(local.audience, "https://"))
      error_message = "tool.yaml: backend.url must be the https MCP endpoint; backend.auth none or managed_identity (with backend.audience)."
    }
    precondition {
      condition     = length(local.tool_names) > 0 && alltrue([for x in try(local.t.tools, []) : try(x.name, "") != "" && contains(["read", "write"], try(x.impact, ""))]) && length(distinct(local.tool_names)) == length(local.tool_names) && local.rate > 0
      error_message = "tool.yaml: tools must list each exposed MCP tool once with impact read or write; gateway.rate_limit_per_minute > 0."
    }
    precondition {
      condition     = alltrue([for s in local.shared_with : can(regex("^[a-z0-9][a-z0-9-]{1,62}$", s)) || can(regex("^/subscriptions/[^/]+/resource[gG]roups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+/projects/[^/]+$", s))]) && !contains(local.foundry_names, local.owner_foundry_project)
      error_message = "tool.yaml shared_with: Foundry project names (this environment's agent account) or full Foundry project resource IDs, not the owner's own project."
    }
  }
}

## The tool on the gateway: https://<apim>/tools/<project>/<tool>/mcp

resource "azurerm_api_management_api" "tool" {
  name                  = local.api_name
  display_name          = "Tool - ${var.project} - ${try(local.t.display_name, var.tool)}"
  description           = "${try(local.t.description, "")} Owner: ${try(local.t.owner, "")} (project ${var.project}). Called by Foundry with the identity of the Foundry projects ${join(", ", concat([local.owner_foundry_project], local.shared_with))}; no subscription key."
  path                  = local.path
  service_url           = local.backend_base
  protocols             = ["https"]
  revision              = "1"
  subscription_required = false
  api_management_name   = data.azurerm_api_management.this.name
  resource_group_name   = data.azurerm_api_management.this.resource_group_name

  depends_on = [terraform_data.validated]
}

# MCP streamable HTTP: POST (messages), GET (server stream), DELETE (end session), all on /mcp.
resource "azurerm_api_management_api_operation" "tool" {
  for_each = toset(["POST", "GET", "DELETE"])

  operation_id        = "mcp-${lower(each.key)}"
  api_name            = azurerm_api_management_api.tool.name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  display_name        = "MCP ${each.key}"
  method              = each.key
  url_template        = "/mcp"
  description         = "MCP streamable HTTP endpoint of ${var.tool}."
}

# The tool's kill switch. Created "active"; after that the value belongs to operations (APIM > Named values), so an
# apply never re-activates a suspended tool.
resource "azurerm_api_management_named_value" "kill_switch" {
  name                = local.kill_switch
  display_name        = local.kill_switch
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = "active"
  secret              = false
  tags                = ["kill-switch", "tool"]

  lifecycle {
    ignore_changes = [value]
  }
}

# Tool registry: the contract consumers' onboarding reads (any repository): endpoint, exposed tools and their impact,
# and which Foundry projects may use it.
resource "azurerm_api_management_named_value" "meta" {
  name                = "tool-meta-${var.project}-${var.tool}"
  display_name        = "tool-meta-${var.project}-${var.tool}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value = jsonencode({
    project          = var.project
    name             = var.tool
    owner            = try(local.t.owner, "")
    display_name     = try(local.t.display_name, var.tool)
    description      = try(local.t.description, "")
    type             = "mcp"
    endpoint         = local.endpoint
    backend          = local.backend_url
    auth             = local.backend_auth
    tools            = [for x in local.t.tools : { name = x.name, impact = x.impact }]
    foundry_projects = concat([local.owner_foundry_project], local.shared_with)
    kill_switch      = local.kill_switch
  })
  secret = false
  tags   = ["tool-meta"]
}

resource "azurerm_api_management_api_policy" "tool" {
  api_name            = azurerm_api_management_api.tool.name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  xml_content = templatefile("${path.module}/policies/tool-api.xml.tftpl", {
    project          = var.project
    tool             = var.tool
    callers          = join("|", local.callers)
    kill_switch      = local.kill_switch
    tool_names       = join("|", local.tool_names)
    rate             = local.rate
    backend_leaf     = local.backend_leaf
    backend_auth     = local.backend_auth
    backend_audience = local.audience
  })

  # The policy reads the kill switch ({{...}}): APIM refuses a policy whose named value doesn't exist yet.
  depends_on = [azurerm_api_management_named_value.kill_switch]
}

resource "azurerm_api_management_product_api" "tool" {
  product_id          = local.env.gateway.models_product
  api_name            = azurerm_api_management_api.tool.name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}
