## Project-owned tools: projects/<project>/tools/<tool>/tool.yaml. A tool belongs to the project that defines it;
## its owner can share it with other projects in the same environment (shared_with). For each tool it owns, the
## project gets a gateway API (tools/<project>/<tool>), the tool's kill switch and its registry entry. For each tool it
## may use (its own and those shared with it), a RemoteTool connection in its Foundry project. Access from APIM to the
## tool's backend, when it needs credentials, is granted by the team that owns the tool, not by this pipeline.

locals {
  tool_files = fileset(local.projects_root, "*/tools/*/tool.yaml")

  # Every tool in the repository: "<owner project>/<tool>" => definition.
  all_tools = {
    for f in local.tool_files : "${split("/", f)[0]}/${split("/", f)[2]}" => merge(yamldecode(file("${local.projects_root}/${f}")), {
      owner_project = split("/", f)[0]
      folder        = split("/", f)[2]
      environment   = try(yamldecode(file("${local.projects_root}/${split("/", f)[0]}/project.yaml")).environment, "test")
    })
  }

  # Tools this project owns, by folder name.
  own_tools = { for k, t in local.all_tools : t.folder => t if t.owner_project == var.project }

  # Tools this project's agents may use: its own and those shared with it.
  usable_tools = { for k, t in local.all_tools : k => t if t.owner_project == var.project || contains(try(t.shared_with, []), var.project) }

  # Foundry project resource ID (token claim xms_mirid) of any repo project, lowercase.
  mirid_prefix = lower("/subscriptions/${local.foundry.subscription_id}/resourcegroups/${local.foundry.resource_group}/providers/Microsoft.CognitiveServices/accounts/${local.foundry.account_name}/projects/")
  project_mirids = {
    for f in fileset(local.projects_root, "*/project.yaml") : dirname(f) =>
    "${local.mirid_prefix}${try(yamldecode(file("${local.projects_root}/${f}")).foundry.project, dirname(f))}"
  }

  tools = {
    for name, t in local.own_tools : name => {
      name          = name
      api           = "tool-${var.project}-${name}"
      path          = "tools/${var.project}/${name}"
      kill_switch   = "ks-tool-${var.project}-${name}"
      backend_base  = try(regex("^(https://.+)/([^/?#]+)$", t.backend.url)[0], "")
      endpoint_leaf = try(regex("^(https://.+)/([^/?#]+)$", t.backend.url)[1], "")
      backend_auth  = try(t.backend.auth, "none")
      audience      = try(t.backend.audience, "")
      tool_names    = [for x in try(t.tools, []) : x.name]
      rate          = try(t.gateway.rate_limit_per_minute, 0)
      shared_with   = try(t.shared_with, [])
      callers       = [for p in concat([var.project], try(t.shared_with, [])) : lookup(local.project_mirids, p, "")]
      definition    = t
    }
  }

  # Connection name in the Foundry project: "<project>-<tool>" for its own tools, "<project>-<owner>-<tool>" for a
  # tool shared with it (names are unique per agent account, so they carry the using project). The agent pipeline
  # derives the same names (scripts/agent_pipeline.py tool_connection).
  tool_connections = {
    for k, t in local.usable_tools : k => {
      name = t.owner_project == var.project ? "${var.project}-${t.folder}" : "${var.project}-${t.owner_project}-${t.folder}"
      url  = "${data.azurerm_api_management.this.gateway_url}/tools/${t.owner_project}/${t.folder}/${try(regex("^(https://.+)/([^/?#]+)$", t.backend.url)[1], "")}"
    }
  }

  tool_fragments = distinct(flatten(regexall("fragment-id=\"([^\"]+)\"", file("${path.module}/policies/tool-api.xml.tftpl"))))
}

resource "terraform_data" "tools_validated" {
  input = keys(local.tools)

  lifecycle {
    precondition {
      condition     = length(local.tools) == 0 || length(setsubtract(local.tool_fragments, local.deployed_fragments)) == 0
      error_message = "Shared policy fragment(s) for tools missing on the gateway: ${join(", ", setsubtract(local.tool_fragments, local.deployed_fragments))}. Run the ai-gateway repository's gateway-policy-deployment workflow for this environment first."
    }
    precondition {
      condition = alltrue([for n, t in local.tools :
        try(t.definition.name, "") == n && can(regex("^[a-z0-9][a-z0-9-]{1,40}$", n)) && try(t.definition.type, "") == "mcp" &&
        t.backend_base != "" && contains(["none", "managed_identity"], t.backend_auth) &&
        (t.backend_auth != "managed_identity" || startswith(t.audience, "api://") || startswith(t.audience, "https://")) &&
        length(t.tool_names) > 0 && alltrue([for x in try(t.definition.tools, []) : contains(["read", "write"], try(x.impact, ""))]) &&
      t.rate > 0])
      error_message = "projects/${var.project}/tools/<tool>/tool.yaml: name must match the folder (lowercase, digits, hyphens), type mcp, backend.url an https MCP endpoint, backend.auth none or managed_identity (with backend.audience), at least one tool with impact read or write, gateway.rate_limit_per_minute > 0."
    }
    precondition {
      condition = alltrue(flatten([for n, t in local.tools : [for p in t.shared_with :
      p != var.project && contains(keys(local.project_mirids), p) && try(yamldecode(file("${local.projects_root}/${p}/project.yaml")).environment, "test") == var.environment]]))
      error_message = "tool.yaml shared_with: each entry must be another project folder in environment ${var.environment}."
    }
    precondition {
      condition     = !contains([for c in values(local.tool_connections) : c.name], local.connection) && length(distinct([for c in values(local.tool_connections) : c.name])) == length(local.tool_connections)
      error_message = "Tool connection names collide with each other or with the model connection '${local.connection}': rename a tool folder."
    }
  }
}

resource "azurerm_api_management_api" "tool" {
  for_each = local.tools

  name                  = each.value.api
  display_name          = "Tool - ${var.project} - ${try(each.value.definition.display_name, each.key)}"
  description           = "${try(each.value.definition.description, "")} Owner: ${try(each.value.definition.owner, "")}. Called by Foundry with the identity of project ${join(", ", concat([var.project], each.value.shared_with))}; no subscription key."
  path                  = each.value.path
  service_url           = each.value.backend_base
  protocols             = ["https"]
  revision              = "1"
  subscription_required = false
  api_management_name   = data.azurerm_api_management.this.name
  resource_group_name   = data.azurerm_api_management.this.resource_group_name

  depends_on = [terraform_data.tools_validated]
}

# MCP streamable HTTP: POST (messages), GET (server stream), DELETE (end session), all on the endpoint.
resource "azurerm_api_management_api_operation" "tool" {
  for_each = { for pair in setproduct(keys(local.tools), ["POST", "GET", "DELETE"]) : "${pair[0]}-${lower(pair[1])}" => { tool = pair[0], method = pair[1] } }

  operation_id        = "mcp-${lower(each.value.method)}"
  api_name            = azurerm_api_management_api.tool[each.value.tool].name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  display_name        = "MCP ${each.value.method}"
  method              = each.value.method
  url_template        = "/${local.tools[each.value.tool].endpoint_leaf}"
  description         = "MCP streamable HTTP endpoint of ${each.value.tool}."
}

# The tool's kill switch. Created "active"; after that the value belongs to operations (APIM > Named values), so an
# apply never re-activates a suspended tool.
resource "azurerm_api_management_named_value" "tool_kill_switch" {
  for_each = local.tools

  name                = each.value.kill_switch
  display_name        = each.value.kill_switch
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = "active"
  secret              = false
  tags                = ["kill-switch", "tool"]

  lifecycle {
    ignore_changes = [value]
  }
}

# Tool registry: who owns the tool, what it exposes, who may use it.
resource "azurerm_api_management_named_value" "tool_meta" {
  for_each = local.tools

  name                = "tool-meta-${var.project}-${each.key}"
  display_name        = "tool-meta-${var.project}-${each.key}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value = jsonencode({
    project     = var.project
    name        = each.key
    owner       = try(each.value.definition.owner, "")
    type        = "mcp"
    endpoint    = "${data.azurerm_api_management.this.gateway_url}/${each.value.path}/${each.value.endpoint_leaf}"
    backend     = each.value.definition.backend.url
    auth        = each.value.backend_auth
    tools       = [for x in each.value.definition.tools : { name = x.name, impact = x.impact }]
    shared_with = each.value.shared_with
    kill_switch = each.value.kill_switch
  })
  secret = false
  tags   = ["tool-meta"]
}

resource "azurerm_api_management_api_policy" "tool" {
  for_each = local.tools

  api_name            = azurerm_api_management_api.tool[each.key].name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  xml_content = templatefile("${path.module}/policies/tool-api.xml.tftpl", {
    project          = var.project
    tool             = each.key
    callers          = join("|", each.value.callers)
    kill_switch      = each.value.kill_switch
    tool_names       = join("|", each.value.tool_names)
    rate             = each.value.rate
    backend_auth     = each.value.backend_auth
    backend_audience = each.value.audience
  })

  # The policy reads the kill switch ({{...}}): APIM refuses a policy whose named value doesn't exist yet.
  depends_on = [azurerm_api_management_named_value.tool_kill_switch]
}

resource "azurerm_api_management_product_api" "tool" {
  for_each = local.tools

  product_id          = local.env.gateway.models_product
  api_name            = azurerm_api_management_api.tool[each.key].name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}

# RemoteTool connection in the project's Foundry project, for each tool its agents may use. Foundry calls the
# gateway with the project's managed identity; the agent definition names the connection (project_connection_id).
resource "azapi_resource" "tool_connection" {
  for_each = local.tool_connections

  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview"
  name                      = each.value.name
  parent_id                 = local.project_id
  schema_validation_enabled = false

  body = {
    properties = {
      category      = "RemoteTool"
      group         = "GenericProtocol"
      target        = each.value.url
      authType      = "ProjectManagedIdentity"
      audience      = "https://cognitiveservices.azure.com"
      isSharedToAll = false
      credentials   = {}
      metadata      = { type = "custom_MCP" }
    }
  }

  depends_on = [azurerm_api_management_api_policy.tool, azapi_resource.foundry_project, terraform_data.tools_validated]
}
