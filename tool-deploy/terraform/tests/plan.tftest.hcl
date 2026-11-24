# Plan tests with mocked providers: a tool's gateway API, auth guard, kill switch and registry entry. No Azure access.
variables {
  environment  = "dev"
  project      = "kb-owner"
  tool         = "kb-search"
  projects_dir = "tests/fixtures/projects"
  # Fixture platform coordinates (in the pipeline: GitHub environment variables).
  apim_subscription_id    = "00000000-0000-0000-0000-000000000000"
  apim_resource_group     = "rg"
  apim_name               = "apim"
  foundry_subscription_id = "3471ad5a-d8a8-4a80-b30b-668798733c14"
  foundry_resource_group  = "az-rg-tst-model-npr-01"
  foundry_account         = "az-aif-tst-model-npr-01"
}

mock_provider "azurerm" {
  mock_data "azurerm_api_management" {
    defaults = {
      id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
      name        = "apim"
      gateway_url = "https://apim.azure-api.net"
    }
  }
}

mock_provider "azapi" {
  mock_data "azapi_resource_list" {
    defaults = { output = { value = [{ name = "tool-caller" }, { name = "tool-route" }] } }
  }
}

run "publishes_the_tool_on_the_gateway" {
  command = plan

  assert {
    condition     = azurerm_api_management_api.tool.name == "tool-kb-owner-kb-search" && azurerm_api_management_api.tool.path == "tools/kb-owner/kb-search" && azurerm_api_management_api.tool.service_url == "https://kb.example.com/api" && !azurerm_api_management_api.tool.subscription_required
    error_message = "The tool's API is tools/<project>/<tool>, in front of the backend, without subscription keys."
  }
  assert {
    condition     = alltrue([for m in ["POST", "GET", "DELETE"] : azurerm_api_management_api_operation.tool[m].url_template == "/mcp"])
    error_message = "MCP streamable HTTP: POST, GET and DELETE on /mcp."
  }
  assert {
    condition     = output.endpoint == "https://apim.azure-api.net/tools/kb-owner/kb-search/mcp"
    error_message = "Consumers connect to <gateway>/tools/<project>/<tool>/mcp."
  }
  assert {
    condition     = azurerm_api_management_named_value.kill_switch.name == "ks-tool-kb-owner-kb-search" && azurerm_api_management_named_value.kill_switch.value == "active"
    error_message = "Each tool has its own kill switch, created active."
  }
}

run "access_per_agent_and_shared_with" {
  command = plan

  assert {
    condition     = jsondecode(azurerm_api_management_named_value.meta.value).role == "tool.kb-owner.kb-search" && !strcontains(azurerm_api_management_api_policy.tool.xml_content, "tool-callers") && strcontains(azurerm_api_management_api_policy.tool.xml_content, "<set-variable name=\"project\" value=\"kb-owner\" />") && strcontains(azurerm_api_management_api_policy.tool.xml_content, "<set-variable name=\"tool\" value=\"kb-search\" />")
    error_message = "Calls are authorized per agent (role tool.<project>.<tool>, checked by tool-caller from the project and tool variables); the registry names the role."
  }
  assert {
    condition     = jsondecode(azurerm_api_management_named_value.meta.value).foundry_projects[0] == "kb-foundry" && length(jsondecode(azurerm_api_management_named_value.meta.value).foundry_projects) == 3
    error_message = "The registry entry lists the Foundry projects that may use the tool (consumers' onboarding checks it)."
  }
  assert {
    condition     = jsondecode(azurerm_api_management_named_value.meta.value).endpoint == "https://apim.azure-api.net/tools/kb-owner/kb-search/mcp" && jsondecode(azurerm_api_management_named_value.meta.value).tools[1].impact == "write"
    error_message = "The registry entry carries the endpoint and the exposed tools with their impact."
  }
}

run "backend_identity_and_path" {
  command = plan

  assert {
    condition = alltrue([for s in [
      "<rewrite-uri template=\"/stream\" copy-unmatched-params=\"true\" />",
      "<authentication-managed-identity resource=\"api://kb-fixture\" />",
      "{{ks-tool-kb-owner-kb-search}}", "value=\"kb_search|kb_update\"", "@(30)",
      "fragment-id=\"tool-caller\"", "fragment-id=\"tool-route\"", "<forward-request buffer-response=\"false\" />",
    ] : strcontains(azurerm_api_management_api_policy.tool.xml_content, s)])
    error_message = "The policy: kill switch, exposed tools, rate, backend path and identity, streaming."
  }
}

run "rejects_invalid_shared_with" {
  command = plan

  variables {
    tool = "bad-share"
  }

  expect_failures = [terraform_data.validated]
}

run "rejects_missing_fragments" {
  command = plan

  override_data {
    target = data.azapi_resource_list.policy_fragments
    values = { output = { value = [{ name = "tool-caller" }] } }
  }

  expect_failures = [terraform_data.validated]
}
