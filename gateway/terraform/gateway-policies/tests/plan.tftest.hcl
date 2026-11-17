# Plan test with mocked providers: every file in gateway/policy-fragments/ becomes an APIM policy fragment, and every
# fragment a policy template includes has a file. No Azure access. Runs against the sandbox environment file, which
# has the Content Safety account (gateway.content_safety).
variables {
  environment = "sandbox"
}

mock_provider "azapi" {}

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
      kind     = "ContentSafety"
      name     = "cs-sbx-bbf0o"
      endpoint = "https://cs-sbx-bbf0o.cognitiveservices.azure.com/"
    }
  }
}

run "deploys_shared_policy_fragments" {
  command = plan

  assert {
    condition = alltrue([for id in ["agent-guardrails", "agent-pin-version", "project-inbound", "project-outbound", "model-api-caller", "model-api-route", "model-content-safety"] :
    contains(keys(azurerm_api_management_policy_fragment.shared), id)])
    error_message = "Every fragment the policy templates include should be deployed."
  }
  assert {
    condition     = alltrue([for f in azurerm_api_management_policy_fragment.shared : f.format == "rawxml"])
    error_message = "Fragments are stored as raw XML (no re-serialisation drift)."
  }
  assert {
    condition     = strcontains(azurerm_api_management_policy_fragment.shared["agent-guardrails"].value, "AI_ROUTE_SUSPENDED")
    error_message = "agent-guardrails should carry the kill switch."
  }
  assert {
    condition     = azurerm_api_management_named_value.tenant_id.value == "fcf67057-50c9-4ad4-98f3-ffca64add9e9"
    error_message = "gateway-tenant-id should be the environment's tenant (sandbox.yaml)."
  }
  assert {
    condition     = azurerm_api_management_named_value.kill_switch_all.name == "ks-gateway-all" && azurerm_api_management_named_value.kill_switch_all.value == "active"
    error_message = "The global kill switch ks-gateway-all starts active."
  }
  assert {
    condition = alltrue([for id in ["agent-guardrails", "model-api-route"] :
    strcontains(azurerm_api_management_policy_fragment.shared[id].value, "{{ks-gateway-all}}")])
    error_message = "agent-guardrails and model-api-route should check the global kill switch."
  }
  assert {
    condition     = azapi_resource.content_safety_backend.name == "content-safety" && azapi_resource.content_safety_backend.body.properties.url == "https://cs-sbx-bbf0o.cognitiveservices.azure.com/"
    error_message = "The content-safety backend should point at the environment's Content Safety account."
  }
  assert {
    condition     = azapi_resource.content_safety_backend.body.properties.credentials.managedIdentity.resource == "https://cognitiveservices.azure.com"
    error_message = "APIM should call Content Safety with its managed identity (no keys)."
  }
  assert {
    condition     = strcontains(azurerm_api_management_policy_fragment.shared["model-content-safety"].value, "backend-id=\"content-safety\"")
    error_message = "model-content-safety should use the content-safety backend."
  }
}

run "requires_a_content_safety_account" {
  command = plan

  variables {
    environment = "test" # test.yaml has no gateway.content_safety
  }

  expect_failures = [data.azurerm_cognitive_account.content_safety]
}
