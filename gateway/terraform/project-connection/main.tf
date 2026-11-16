## Validation against the registry in APIM (fails the plan before anything changes)

resource "terraform_data" "validated" {
  input = keys(local.models)

  lifecycle {
    precondition {
      condition     = length(local.allowed) > 0
      error_message = "projects/${var.project}/project.yaml lists no models (models.allowed)."
    }
    precondition {
      condition     = alltrue([for n in keys(local.allowed) : contains(keys(local.registry), n)])
      error_message = "Not registered on the gateway: ${join(", ", [for n in keys(local.allowed) : n if !contains(keys(local.registry), n)])}. Registered: ${join(", ", keys(local.registry))}."
    }
    precondition {
      condition     = alltrue([for n, m in local.allowed : m.tokens_per_minute > 0 && m.tokens_per_day >= m.tokens_per_minute])
      error_message = "Each allowed model needs tokens_per_minute > 0 and tokens_per_day >= tokens_per_minute."
    }
    precondition {
      condition = alltrue([
        for n, m in local.allowed : !contains(keys(local.registry), n) ||
        local.allocated_by_others[n] + m.tokens_per_minute <= try(local.registry[n].capacity_tpm, 0)
      ])
      error_message = "Not enough capacity: ${join("; ", [for n, m in local.allowed : "${n} asks ${m.tokens_per_minute} TPM, free ${try(local.registry[n].capacity_tpm, 0) - local.allocated_by_others[n]}" if contains(keys(local.registry), n)])}."
    }
  }
}

## The project's model API: https://<apim>/models/<project>/deployments/<model>/chat/completions
## Only this project's Foundry identity may call it (token claim xms_mirid); limits per model in the policy.

resource "azurerm_api_management_api" "models" {
  name                  = "models-${var.project}"
  display_name          = "Models - ${var.project}"
  path                  = "models/${var.project}"
  protocols             = ["https"]
  revision              = "1"
  subscription_required = false
  api_management_name   = data.azurerm_api_management.this.name
  resource_group_name   = data.azurerm_api_management.this.resource_group_name

  # API reference in the Developer Portal: description (the project's models and limits) and the operation.
  import {
    content_format = "openapi+json"
    content_value  = jsonencode(local.openapi)
  }

  depends_on = [terraform_data.validated]
}

resource "azurerm_api_management_api_policy" "models" {
  api_name            = azurerm_api_management_api.models.name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  xml_content = templatefile("${path.module}/policies/project-models-api.xml.tftpl", {
    tenant_id     = local.env.tenant_id
    project       = var.project
    project_mirid = local.project_mirid
    models        = values(local.models)
  })
}

resource "azurerm_api_management_product_api" "models" {
  product_id          = local.env.gateway.models_product
  api_name            = azurerm_api_management_api.models.name
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}

## The project's allocation per model, read by the other projects' capacity check

resource "azurerm_api_management_named_value" "limit" {
  for_each = local.models

  name                = "model-limit-${var.project}-${replace(each.key, ".", "-")}"
  display_name        = "model-limit-${var.project}-${replace(each.key, ".", "-")}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value = jsonencode({
    project           = var.project
    model             = each.key
    tokens_per_minute = each.value.tokens_per_minute
    tokens_per_day    = each.value.tokens_per_day
  })
  secret = false
  tags   = ["model-limit"]
}

## AI Gateway connection in the project: its agents use "<connection>/<model>"

resource "azapi_resource" "connection" {
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview"
  name                      = local.connection
  parent_id                 = local.project_id
  schema_validation_enabled = false

  body = {
    properties = {
      category      = "ApiManagement"
      target        = "${data.azurerm_api_management.this.gateway_url}/models/${var.project}"
      authType      = "ProjectManagedIdentity"
      audience      = "https://cognitiveservices.azure.com"
      isSharedToAll = false
      credentials   = {}
      metadata = {
        deploymentInPath    = "true"
        inferenceAPIVersion = var.inference_api_version
        # Static model list (no discovery calls): only the models this project is allowed to use.
        models = jsonencode([for m in values(local.models) : {
          name       = m.name
          properties = { model = { name = m.model, version = m.version, format = m.format } }
        }])
      }
    }
  }

  depends_on = [azurerm_api_management_api_policy.models]
}

# The chat operation now comes from the imported OpenAPI spec: forget the earlier separate operation resource
# without deleting it (the import keeps the same operation ID).
removed {
  from = azurerm_api_management_api_operation.chat
  lifecycle {
    destroy = false
  }
}
