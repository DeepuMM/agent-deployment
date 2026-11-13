# Plan test with mocked providers: the model definitions in gateway/foundry-accounts/ become a deployment,
# a backend, a registry entry and a kill switch per model. No Azure access.
mock_provider "azurerm" {
  mock_data "azurerm_api_management" {
    defaults = {
      id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
      name        = "apim"
      gateway_url = "https://apim.azure-api.net"
    }
  }
  mock_data "azurerm_cognitive_account" {
    defaults = {
      id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct"
      endpoint = "https://acct.cognitiveservices.azure.com/"
    }
  }
}

run "registers_each_model" {
  command = plan

  # gpt-5.4-mini is adopted with an import block; mock providers can't import, so the result is overridden.
  override_resource {
    target = azurerm_cognitive_deployment.model
  }

  assert {
    condition     = contains(keys(azurerm_cognitive_deployment.model), "gpt-5.4-mini")
    error_message = "gpt-5.4-mini should be deployed."
  }
  assert {
    condition     = azurerm_api_management_backend.model["gpt-5.4-mini"].url == "https://acct.cognitiveservices.azure.com/openai"
    error_message = "Backend should point at the hosting account's /openai endpoint."
  }
  assert {
    condition     = jsondecode(azurerm_api_management_named_value.registry["gpt-5.4-mini"].value).capacity_tpm == 250000
    error_message = "Registry capacity should be deployment capacity x 1000 TPM."
  }
  assert {
    condition     = azurerm_api_management_named_value.kill_switch["gpt-5.4-mini"].value == "active"
    error_message = "Kill switch starts active."
  }
}
