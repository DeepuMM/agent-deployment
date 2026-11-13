data "azurerm_api_management" "this" {
  name                = local.env.gateway.apim_name
  resource_group_name = local.env.gateway.resource_group
}

# All named values of the gateway: the model registry (model-meta-*) and every project's limits (model-limit-*).
data "azapi_resource_list" "named_values" {
  type                   = "Microsoft.ApiManagement/service/namedValues@2024-05-01"
  parent_id              = data.azurerm_api_management.this.id
  response_export_values = ["value"]
}
