## Shared gateway policy fragments, included by every project API, agent operation and project model API policy.
## A change here reaches all of them on this APIM at once, without redeploying any agent or project.

# The environment's Entra tenant, for the token check in the model-api-caller fragment.
resource "azurerm_api_management_named_value" "tenant_id" {
  name                = "gateway-tenant-id"
  display_name        = "gateway-tenant-id"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = local.env.tenant_id
  secret              = false
  tags                = ["policy-fragments"]
}

# Global kill switch, checked by agent-guardrails and model-api-route: anything other than "active" stops every
# agent and model API on this gateway. Created "active"; after that the value belongs to operations (APIM > Named
# values), so an apply never re-activates a suspended gateway.
resource "azurerm_api_management_named_value" "kill_switch_all" {
  name                = "ks-gateway-all"
  display_name        = "ks-gateway-all"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = "active"
  secret              = false
  tags                = ["kill-switch", "policy-fragments"]

  lifecycle {
    ignore_changes = [value]
  }
}

resource "azurerm_api_management_policy_fragment" "shared" {
  for_each = local.policy_fragments

  api_management_id = data.azurerm_api_management.this.id
  name              = each.key
  format            = "rawxml" # stored as written: no re-serialisation, so no plan drift
  value             = each.value
  description       = "Shared AI gateway policy, managed in gateway/policy-fragments/${each.key}.xml"

  # Preconditions, not a check block: a missing or malformed fragment must stop the apply, not just warn.
  lifecycle {
    precondition {
      condition     = can(regex("^[A-Za-z0-9-]{1,80}$", each.key))
      error_message = "Policy fragment file names (gateway/policy-fragments/<id>.xml): letters, digits and hyphens, at most 80."
    }
    precondition {
      condition     = startswith(trimspace(each.value), "<fragment>")
      error_message = "Policy fragment ${each.key}: the root element must be <fragment>."
    }
    precondition {
      condition     = length(setsubtract(local.referenced_fragments, keys(local.policy_fragments))) == 0
      error_message = "A policy template includes policy fragment(s) with no file in gateway/policy-fragments/: ${join(", ", setsubtract(local.referenced_fragments, keys(local.policy_fragments)))}."
    }
  }

  # Fragments reference named values ({{...}}): APIM refuses to save one whose named value doesn't exist yet.
  depends_on = [azurerm_api_management_named_value.tenant_id, azurerm_api_management_named_value.kill_switch_all]
}
