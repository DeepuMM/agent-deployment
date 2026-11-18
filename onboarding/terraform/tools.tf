## Tools this project's agents use (project.yaml tools.use: ["<owner project>/<tool>", ...]). Tools are published on
## the gateway by their owner's tool-deploy pipeline, in this repository or another; this stack only connects the
## project to them. Everything comes from the gateway's tool registry (named values tool-meta-<owner>-<tool>): the
## tool must exist and list this project's Foundry project in foundry_projects (the owner's shared_with), otherwise
## the plan stops. The gateway enforces the same list on every call.

locals {
  tools_use = [for u in try(local.project.tools.use, []) : tostring(u)]

  # Tool registry, read from APIM: "<owner>/<tool>" => entry.
  tool_registry = {
    for name, p in local.named_values :
    "${jsondecode(p.value).project}/${jsondecode(p.value).name}" => jsondecode(p.value)
    if startswith(name, "tool-meta-")
  }

  tools_missing    = [for u in local.tools_use : u if !contains(keys(local.tool_registry), u)]
  tools_not_shared = [for u in local.tools_use : u if contains(keys(local.tool_registry), u) && !contains(try(local.tool_registry[u].foundry_projects, []), local.foundry_project)]

  # Connection name in the Foundry project (names are unique per agent account, so they carry the using project):
  # "<project>-<tool>" for a tool the project owns, "<project>-<owner>-<tool>" otherwise. The agent pipeline derives
  # the same names (scripts/agent_pipeline.py tool_connection).
  tool_connections = {
    for u in local.tools_use : u => {
      name = split("/", u)[0] == var.project ? "${var.project}-${split("/", u)[1]}" : "${var.project}-${split("/", u)[0]}-${split("/", u)[1]}"
      url  = try(local.tool_registry[u].endpoint, "")
    }
  }
}

resource "terraform_data" "tools_validated" {
  input = local.tools_use

  lifecycle {
    precondition {
      condition     = alltrue([for u in local.tools_use : can(regex("^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$", u))]) && length(distinct(local.tools_use)) == length(local.tools_use)
      error_message = "projects/${var.project}/project.yaml tools.use: each entry once, as <owner project>/<tool>."
    }
    precondition {
      condition     = length(local.tools_missing) == 0
      error_message = "Not published on the gateway: ${join(", ", local.tools_missing)}. The owner's tool-deploy pipeline publishes a tool (projects/<owner>/tools/<tool>/)."
    }
    precondition {
      condition     = length(local.tools_not_shared) == 0
      error_message = "Not shared with Foundry project '${local.foundry_project}': ${join(", ", local.tools_not_shared)}. Ask the tool's owner to add it to shared_with in tool.yaml."
    }
    precondition {
      condition     = !contains([for c in values(local.tool_connections) : c.name], local.connection)
      error_message = "A tool connection name collides with the model connection '${local.connection}'."
    }
  }
}

# RemoteTool connection in the project's Foundry project, per tool used. Foundry calls the gateway with the project's
# managed identity; the agent definition names the connection (project_connection_id).
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

  depends_on = [azapi_resource.foundry_project, terraform_data.tools_validated]
}

# The tool's gateway resources moved to the tool-deploy stack (state tool-<project>-<tool>-<env>.tfstate): this stack
# forgets them without deleting them.
removed {
  from = azurerm_api_management_api.tool
  lifecycle {
    destroy = false
  }
}
removed {
  from = azurerm_api_management_api_operation.tool
  lifecycle {
    destroy = false
  }
}
removed {
  from = azurerm_api_management_named_value.tool_kill_switch
  lifecycle {
    destroy = false
  }
}
removed {
  from = azurerm_api_management_named_value.tool_meta
  lifecycle {
    destroy = false
  }
}
removed {
  from = azurerm_api_management_api_policy.tool
  lifecycle {
    destroy = false
  }
}
removed {
  from = azurerm_api_management_product_api.tool
  lifecycle {
    destroy = false
  }
}
