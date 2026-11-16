# Plan test with mocked providers: every file in gateway/policy-fragments/ becomes an APIM policy fragment, and every
# fragment a policy template includes has a file. No Azure access.
mock_provider "azurerm" {
  mock_data "azurerm_api_management" {
    defaults = {
      id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
      name        = "apim"
      gateway_url = "https://apim.azure-api.net"
    }
  }
}

run "deploys_shared_policy_fragments" {
  command = plan

  assert {
    condition = alltrue([for id in ["agent-guardrails", "agent-pin-version", "project-inbound", "project-outbound", "model-api-caller", "model-api-route"] :
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
    condition     = azurerm_api_management_named_value.tenant_id.value == "7b180d87-6e3e-4bc3-9372-0ab40c6997bb"
    error_message = "gateway-tenant-id should be the environment's tenant (test.yaml)."
  }
}
