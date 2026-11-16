# The APIM instance is owned by another stack: looked up, never changed.
data "azurerm_api_management" "this" {
  name                = local.env.gateway.apim_name
  resource_group_name = local.env.gateway.resource_group
}
