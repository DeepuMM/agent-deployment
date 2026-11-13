## Validation: catches a wrong model definition before anything is changed

check "model_definitions" {
  assert {
    condition     = alltrue([for n, m in local.models : m.name == n])
    error_message = "model.yaml 'name' must equal its folder name (the gateway model name)."
  }
  assert {
    condition     = alltrue([for n, m in local.models : can(regex("^[a-z0-9][a-z0-9.-]{0,62}$", n))])
    error_message = "Gateway model names: lowercase letters, digits, dots and hyphens, at most 63 characters."
  }
  assert {
    condition     = alltrue([for n, m in local.models : contains(local.accounts[m.account].formats, m.model.format)])
    error_message = "A model's format must be one of its hosting account's formats (account.yaml)."
  }
  assert {
    condition     = alltrue([for n, m in local.models : contains(["openai"], m.api) && contains(["active", "deprecated"], m.status)])
    error_message = "api must be 'openai'; status must be 'active' or 'deprecated'."
  }
}

## Model deployment in its hosting Foundry account

resource "azurerm_cognitive_deployment" "model" {
  for_each = local.models

  name                   = each.value.deployment.name
  cognitive_account_id   = data.azurerm_cognitive_account.hosting[each.value.account].id
  rai_policy_name        = each.value.guardrail
  version_upgrade_option = each.value.deployment.version_upgrade

  model {
    format  = each.value.model.format
    name    = each.value.model.name
    version = each.value.model.version
  }

  sku {
    name     = each.value.deployment.sku
    capacity = each.value.deployment.capacity
  }
}

# Deployments that existed before this pipeline are adopted, not recreated (deployment.existing: true).
import {
  for_each = { for n, m in local.models : n => m if try(m.deployment.existing, false) }
  to       = azurerm_cognitive_deployment.model[each.key]
  id       = "${data.azurerm_cognitive_account.hosting[each.value.account].id}/deployments/${each.value.deployment.name}"
}

## Gateway backend: the hosting account's Azure OpenAI endpoint. APIM authenticates with its managed identity
## (policy in the model-access stack); its identity needs Foundry User on the hosting account.

resource "azurerm_api_management_backend" "model" {
  for_each = local.models

  name                = each.value.backend_id
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  protocol            = "http"
  url                 = "${trimsuffix(data.azurerm_cognitive_account.hosting[each.value.account].endpoint, "/")}/openai"
  description         = "Model ${each.key}: deployment ${each.value.deployment.name} in ${each.value.account}"

  depends_on = [azurerm_cognitive_deployment.model]
}

## Registry entry (read by model-access and project pipelines) and kill switch per model

resource "azurerm_api_management_named_value" "registry" {
  for_each = local.registry

  name                = "model-meta-${local.models[each.key].key}"
  display_name        = "model-meta-${local.models[each.key].key}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = jsonencode(each.value)
  secret              = false
  tags                = ["model-registry"]

  depends_on = [azurerm_api_management_backend.model]
}

resource "azurerm_api_management_named_value" "kill_switch" {
  for_each = local.models

  name                = "ks-model-${each.value.key}"
  display_name        = "ks-model-${each.value.key}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = "active"
  secret              = false
  tags                = ["kill-switch", "model"]

  lifecycle {
    # After creation the value belongs to operations (APIM > Named values, or the kill-switch workflow),
    # so an apply never re-activates a suspended model.
    ignore_changes = [value]
  }
}

## Product: every project's model API is added to it (model-access stack)

resource "azurerm_api_management_product" "models" {
  product_id            = local.env.gateway.models_product
  display_name          = "Platform models"
  description           = local.catalog
  api_management_name   = data.azurerm_api_management.this.name
  resource_group_name   = data.azurerm_api_management.this.resource_group_name
  subscription_required = true
  approval_required     = true
  subscriptions_limit   = 100 # required with approval_required
  published             = true
}

# Signed-in developers see the product (and its catalog) in the Developer Portal.
resource "azurerm_api_management_product_group" "developers" {
  product_id          = azurerm_api_management_product.models.product_id
  group_name          = "developers"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}
