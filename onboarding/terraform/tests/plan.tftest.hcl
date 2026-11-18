# Plan tests with mocked providers: the registry (model-meta-*) and other projects' limits (model-limit-*) are
# mocked as if read from APIM. No Azure access.
variables {
  project     = "models-demo"
  environment = "test"
  # Fixture platform coordinates (in the pipeline: GitHub environment variables).
  apim_subscription_id    = "00000000-0000-0000-0000-000000000000"
  apim_resource_group     = "rg"
  apim_name               = "apim"
  foundry_subscription_id = "8c85e375-8177-4a9c-8389-441a77137e47"
  foundry_resource_group  = "rg-ai-gateway-sandbox"
  foundry_account         = "aif-sbx-agents-bbf0o"
}

# models-demo adopts its existing Foundry project (foundry.existing: true); mocked providers can't import, so the
# project's planned state is overridden.
override_resource {
  target = azapi_resource.foundry_project
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

# Default for every azapi_resource_list: the shared policy fragments are deployed. Each run overrides named_values.
mock_provider "azapi" {
  mock_data "azapi_resource" {
    defaults = { location = "eastus2" }
  }
  mock_data "azapi_resource_list" {
    defaults = {
      output = { value = [{ name = "model-api-caller" }, { name = "model-api-route" }, { name = "project-outbound" }, { name = "model-content-safety" }, { name = "tool-caller" }, { name = "tool-route" }] }
    }
  }
}

run "creates_the_foundry_project" {
  command = plan

  variables {
    project      = "cs-fixture" # tests/fixtures/projects/cs-fixture: foundry.project shared-foundry, not adopted
    projects_dir = "tests/fixtures/projects"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "model-meta-gpt-x", properties = { value = "{\"name\":\"gpt-x\",\"backend_id\":\"bx\",\"deployment\":\"gpt-x\",\"kill_switch\":\"ksx\",\"model\":\"gpt-x\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  assert {
    condition     = azapi_resource.foundry_project["shared-foundry"].parent_id == "/subscriptions/8c85e375-8177-4a9c-8389-441a77137e47/resourceGroups/rg-ai-gateway-sandbox/providers/Microsoft.CognitiveServices/accounts/aif-sbx-agents-bbf0o"
    error_message = "The Foundry project is created in the environment's agent account."
  }
  assert {
    condition     = azapi_resource.foundry_project["shared-foundry"].location == "eastus2" && azapi_resource.foundry_project["shared-foundry"].identity[0].type == "SystemAssigned"
    error_message = "The project sits in the account's region and has its own managed identity (it calls the gateway)."
  }
  assert {
    condition     = output.foundry_project_endpoint == "https://aif-sbx-agents-bbf0o.services.ai.azure.com/api/projects/shared-foundry"
    error_message = "The agent pipeline deploys to this endpoint."
  }
}

run "builds_connection_and_policy_from_registry" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"model-acct-gpt-5.4-mini\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks-model-gpt-5.4-mini\",\"model\":\"gpt-5.4-mini\",\"version\":\"2026-03-17\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "model-limit-other-gpt-5-4-mini", properties = { value = "{\"project\":\"other\",\"model\":\"gpt-5.4-mini\",\"tokens_per_minute\":100000,\"tokens_per_day\":1000000}" } },
        ]
      }
    }
  }

  assert {
    condition     = azapi_resource.connection.body.properties.target == "https://apim.azure-api.net/models/models-demo"
    error_message = "Connection should target the project's model API."
  }
  assert {
    condition     = jsondecode(azapi_resource.connection.body.properties.metadata.models)[0].name == "gpt-5.4-mini"
    error_message = "Connection should list the allowed model."
  }
  assert {
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "name=\"model-tpm\" value=\"@(20000)\"")
    error_message = "Policy should carry the project's token limit."
  }
  assert {
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "name=\"model-backend\" value=\"model-acct-gpt-5.4-mini\"")
    error_message = "Policy should route to the backend from the registry."
  }
  assert {
    condition = alltrue([for id in ["model-api-caller", "model-api-route", "model-content-safety", "project-outbound"] :
    strcontains(azurerm_api_management_api_policy.models.xml_content, "fragment-id=\"${id}\"")])
    error_message = "Policy should include the shared fragments."
  }
  assert {
    condition     = !strcontains(azurerm_api_management_api_policy.models.xml_content, "name=\"cs-")
    error_message = "models-demo sets no content_safety: every setting is left to the fragment's platform default."
  }
}

run "resolves_content_safety_per_model" {
  command = plan

  variables {
    project      = "cs-fixture" # tests/fixtures: project level self_harm: 3; model gpt-x adds violence: 2, hate: 1
    projects_dir = "tests/fixtures/projects"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "model-meta-gpt-x", properties = { value = "{\"name\":\"gpt-x\",\"backend_id\":\"bx\",\"deployment\":\"gpt-x\",\"kill_switch\":\"ksx\",\"model\":\"gpt-x\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  assert {
    condition     = length(regexall("name=\"cs-self-harm\" value=\"@\\(3\\)\"", azurerm_api_management_api_policy.models.xml_content)) == 2
    error_message = "The project-level self_harm: 3 should reach both models' branches."
  }
  assert {
    condition = (
      strcontains(regex("(?s)&quot;gpt-x&quot;\\)\">.*?</when>", azurerm_api_management_api_policy.models.xml_content), "<set-variable name=\"cs-violence\" value=\"@(2)\" />") &&
      strcontains(regex("(?s)&quot;gpt-x&quot;\\)\">.*?</when>", azurerm_api_management_api_policy.models.xml_content), "<set-variable name=\"cs-hate\" value=\"@(1)\" />")
    )
    error_message = "gpt-x's own settings (violence 2, hate 1) belong in gpt-x's branch."
  }
  assert {
    condition = (
      strcontains(regex("(?s)&quot;gpt-x&quot;\\)\">.*?</when>", azurerm_api_management_api_policy.models.xml_content), "<set-variable name=\"cs-check-answers\" value=\"@(true)\" />") &&
      !strcontains(regex("(?s)&quot;gpt-5.4-mini&quot;\\)\">.*?</when>", azurerm_api_management_api_policy.models.xml_content), "cs-check-answers")
    )
    error_message = "check_answers (opt-in) should be set for gpt-x only; gpt-5.4-mini keeps the default (off)."
  }
  assert {
    condition     = !strcontains(regex("(?s)&quot;gpt-5.4-mini&quot;\\)\">.*?</when>", azurerm_api_management_api_policy.models.xml_content), "cs-violence")
    error_message = "gpt-5.4-mini has no model-level settings: it gets only the project level."
  }
  assert {
    condition     = !strcontains(azurerm_api_management_api_policy.models.xml_content, "cs-sexual")
    error_message = "Settings neither the model nor the project passes are left to the fragment's platform default."
  }
  assert {
    condition     = endswith(azapi_resource.connection.parent_id, "/accounts/aif-sbx-agents-bbf0o/projects/shared-foundry")
    error_message = "The AI Gateway connection belongs in the Foundry project of foundry_project_endpoint (shared-foundry), not one named after the folder (cs-fixture)."
  }
  assert {
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "/accounts/aif-sbx-agents-bbf0o/projects/shared-foundry\" />") && azurerm_api_management_api.models.path == "models/cs-fixture"
    error_message = "The identity check uses the endpoint's Foundry project; the model API keeps the repo project's name."
  }
}

run "rejects_two_projects_naming_one_foundry_project" {
  command = plan

  variables {
    project      = "owner-b" # tests/fixtures/conflict: owner-a and owner-b both name Foundry project shared
    projects_dir = "tests/fixtures/conflict"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.validated]
}

run "rejects_when_shared_fragments_are_missing" {
  command = plan

  override_data {
    target = data.azapi_resource_list.policy_fragments
    values = { output = { value = [{ name = "project-outbound" }] } }
  }
  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.validated]
}

run "rejects_a_model_not_registered" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = { output = { value = [] } }
  }

  expect_failures = [terraform_data.validated, terraform_data.tools_validated] # an empty registry has no tools either
}

run "rejects_over_capacity" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "model-limit-other-gpt-5-4-mini", properties = { value = "{\"project\":\"other\",\"model\":\"gpt-5.4-mini\",\"tokens_per_minute\":240000,\"tokens_per_day\":1000000}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.validated]
}


run "connects_a_tool_from_the_registry" {
  command = plan

  variables {
    project      = "tool-user" # project.yaml tools.use: [kb-owner/kb-search], a tool of a project in another repository
    projects_dir = "tests/fixtures/projects"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "tool-meta-kb-owner-kb-search", properties = { value = "{\"project\":\"kb-owner\",\"name\":\"kb-search\",\"endpoint\":\"https://apim.azure-api.net/tools/kb-owner/kb-search/mcp\",\"foundry_projects\":[\"kb-foundry\",\"tool-user\"],\"tools\":[{\"name\":\"kb_search\",\"impact\":\"read\"}]}" } },
        ]
      }
    }
  }

  assert {
    condition     = azapi_resource.tool_connection["kb-owner/kb-search"].name == "tool-user-kb-owner-kb-search" && azapi_resource.tool_connection["kb-owner/kb-search"].body.properties.target == "https://apim.azure-api.net/tools/kb-owner/kb-search/mcp"
    error_message = "A tool of another project gets a connection named <project>-<owner>-<tool>, to the endpoint in the registry."
  }
  assert {
    condition     = azapi_resource.tool_connection["kb-owner/kb-search"].body.properties.category == "RemoteTool" && azapi_resource.tool_connection["kb-owner/kb-search"].body.properties.authType == "ProjectManagedIdentity"
    error_message = "Agents reach the tool through a RemoteTool connection with the project's identity."
  }
}

run "rejects_a_tool_not_shared_with_the_project" {
  command = plan

  variables {
    project      = "tool-user" # project.yaml tools.use: [kb-owner/kb-search], a tool of a project in another repository
    projects_dir = "tests/fixtures/projects"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "tool-meta-kb-owner-kb-search", properties = { value = "{\"project\":\"kb-owner\",\"name\":\"kb-search\",\"endpoint\":\"https://apim.azure-api.net/tools/kb-owner/kb-search/mcp\",\"foundry_projects\":[\"kb-foundry\"],\"tools\":[{\"name\":\"kb_search\",\"impact\":\"read\"}]}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.tools_validated]
}

run "rejects_a_tool_not_published" {
  command = plan

  variables {
    project      = "tool-user" # project.yaml tools.use: [kb-owner/kb-search], a tool of a project in another repository
    projects_dir = "tests/fixtures/projects"
  }

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.tools_validated]
}

run "connects_its_own_tool" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "tool-meta-models-demo-ms-learn", properties = { value = "{\"project\":\"models-demo\",\"name\":\"ms-learn\",\"endpoint\":\"https://apim.azure-api.net/tools/models-demo/ms-learn/mcp\",\"foundry_projects\":[\"models-demo\"],\"tools\":[{\"name\":\"microsoft_docs_search\",\"impact\":\"read\"}]}" } },
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
        ]
      }
    }
  }

  assert {
    condition     = azapi_resource.tool_connection["models-demo/ms-learn"].name == "models-demo-ms-learn"
    error_message = "A project's own tool keeps the connection name <project>-<tool>."
  }
}
