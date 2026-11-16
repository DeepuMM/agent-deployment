# The APIM instance and the hosting Foundry accounts are owned by other stacks: looked up, never changed.
data "azurerm_api_management" "this" {
  name                = local.env.gateway.apim_name
  resource_group_name = local.env.gateway.resource_group
}

data "azurerm_cognitive_account" "hosting" {
  for_each            = local.accounts
  name                = each.key
  resource_group_name = each.value.resource_group
}
