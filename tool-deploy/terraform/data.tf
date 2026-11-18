data "azurerm_api_management" "this" {
  name                = local.env.gateway.apim_name
  resource_group_name = local.env.gateway.resource_group
}

# Shared policy fragments on the gateway (deployed by the ai-gateway repository's gateway-policy-deployment).
data "azapi_resource_list" "policy_fragments" {
  type                   = "Microsoft.ApiManagement/service/policyFragments@2024-05-01"
  parent_id              = data.azurerm_api_management.this.id
  response_export_values = ["value"]
}
