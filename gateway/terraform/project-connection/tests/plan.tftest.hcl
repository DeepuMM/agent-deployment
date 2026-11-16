# Plan tests with mocked providers: the registry (model-meta-*) and other projects' limits (model-limit-*) are
# mocked as if read from APIM. No Azure access.
variables {
  project = "models-demo"
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

mock_provider "azapi" {}

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
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "tokens-per-minute=\"20000\"")
    error_message = "Policy should carry the project's token limit."
  }
  assert {
    condition     = strcontains(azurerm_api_management_api_policy.models.xml_content, "backend-id=\"model-acct-gpt-5.4-mini\"")
    error_message = "Policy should route to the backend from the registry."
  }
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
