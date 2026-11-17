# Plan tests with mocked providers: the registry (model-meta-*) and other projects' limits (model-limit-*) are
# mocked as if read from APIM. No Azure access.
variables {
  project     = "models-demo"
  environment = "sandbox" # models-demo's foundry_project_endpoint is in the sandbox agent account
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
  mock_data "azapi_resource_list" {
    defaults = {
      output = { value = [{ name = "model-api-caller" }, { name = "model-api-route" }, { name = "project-outbound" }, { name = "model-content-safety" }] }
    }
  }
}

run "builds_connection_and_policy_from_registry" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
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

run "passes_project_content_safety_settings" {
  command = plan

  variables {
    project      = "cs-fixture" # tests/fixtures: projects/cs-fixture/project.yaml sets violence: 2 and self_harm: 3
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

  assert {
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "<set-variable name=\"cs-violence\" value=\"@(2)\" />") && strcontains(azurerm_api_management_api_policy.models.xml_content, "<set-variable name=\"cs-self-harm\" value=\"@(3)\" />")
    error_message = "A project's content_safety settings should be passed to the fragment."
  }
  assert {
    condition     = !strcontains(azurerm_api_management_api_policy.models.xml_content, "cs-hate")
    error_message = "Settings the project doesn't pass are left to the fragment's platform default."
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

run "rejects_two_connection_owners_for_one_foundry_project" {
  command = plan

  variables {
    project      = "owner-b" # tests/fixtures/conflict: owner-a and owner-b both have models: in Foundry project shared
    projects_dir = "tests/fixtures/conflict"
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

  expect_failures = [terraform_data.validated]
}

run "rejects_over_capacity" {
  command = plan

  override_data {
    target = data.azapi_resource_list.named_values
    values = {
      output = {
        value = [
          { name = "model-meta-gpt-5-4-mini", properties = { value = "{\"name\":\"gpt-5.4-mini\",\"backend_id\":\"b\",\"deployment\":\"gpt-5.4-mini\",\"kill_switch\":\"ks\",\"model\":\"gpt-5.4-mini\",\"version\":\"v\",\"format\":\"OpenAI\",\"capacity_tpm\":250000}" } },
          { name = "model-limit-other-gpt-5-4-mini", properties = { value = "{\"project\":\"other\",\"model\":\"gpt-5.4-mini\",\"tokens_per_minute\":240000,\"tokens_per_day\":1000000}" } },
        ]
      }
    }
  }

  expect_failures = [terraform_data.validated]
}
