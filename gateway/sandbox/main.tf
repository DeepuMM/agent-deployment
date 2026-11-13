# gateway/sandbox — DISPOSABLE test environment for the model pipelines, in one resource group.
# Creates what platform/environments/sandbox.yaml points at: a model-hosting Foundry account (no agents), an
# agent Foundry account + project (no model deployments), an APIM (Basic v2), and storage for Terraform state.
# The gpt-5.4-mini deployment is NOT created here: the model onboarding pipeline creates it. Local state;
# `terraform destroy` (or deleting the resource group) removes everything.
terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 4.2, < 5.0" }
    azapi   = { source = "Azure/azapi", version = "~> 2.4" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    resource_group { prevent_deletion_if_contains_resources = false }
    cognitive_account { purge_soft_delete_on_destroy = true }
  }
  storage_use_azuread = true
  subscription_id     = var.subscription_id
}

provider "azapi" {}

variable "subscription_id" { type = string }
variable "location" {
  type    = string
  default = "eastus2"
}
variable "publisher_email" { type = string }

data "azurerm_client_config" "me" {}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  s = random_string.suffix.result
}

resource "azurerm_resource_group" "sbx" {
  name     = "rg-ai-gateway-sandbox"
  location = var.location
  tags     = { purpose = "model-pipeline-sandbox", disposable = "true" }
}

## Terraform state for the pipeline stacks

resource "azurerm_storage_account" "state" {
  name                            = "sttfsbx${local.s}"
  resource_group_name             = azurerm_resource_group.sbx.name
  location                        = azurerm_resource_group.sbx.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
}

resource "azurerm_storage_container" "state" {
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.state.id
}

resource "azurerm_role_assignment" "me_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.me.object_id
}

## Model-hosting Foundry account (models only)

resource "azurerm_cognitive_account" "models" {
  name                       = "aif-sbx-models-${local.s}"
  resource_group_name        = azurerm_resource_group.sbx.name
  location                   = azurerm_resource_group.sbx.location
  kind                       = "AIServices"
  sku_name                   = "S0"
  custom_subdomain_name      = "aif-sbx-models-${local.s}"
  local_auth_enabled         = false
  project_management_enabled = true

  identity {
    type = "SystemAssigned"
  }
}

## Agent Foundry account and project (agents only, no model deployments)

resource "azurerm_cognitive_account" "agents" {
  name                       = "aif-sbx-agents-${local.s}"
  resource_group_name        = azurerm_resource_group.sbx.name
  location                   = azurerm_resource_group.sbx.location
  kind                       = "AIServices"
  sku_name                   = "S0"
  custom_subdomain_name      = "aif-sbx-agents-${local.s}"
  local_auth_enabled         = false
  project_management_enabled = true

  identity {
    type = "SystemAssigned"
  }
}

resource "azapi_resource" "project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = "models-demo"
  parent_id = azurerm_cognitive_account.agents.id
  location  = azurerm_resource_group.sbx.location
  identity {
    type = "SystemAssigned"
  }
  body = {
    properties = { displayName = "Models demo (sandbox)", description = "Agents that use platform models through the gateway" }
  }
  response_export_values = ["properties.endpoints"]
}

resource "azurerm_role_assignment" "me_agents" {
  scope                = azurerm_cognitive_account.agents.id
  role_definition_name = "Foundry User"
  principal_id         = data.azurerm_client_config.me.object_id
}

## AI gateway (APIM Basic v2) with a managed identity that may call the hosting account

resource "azurerm_api_management" "gw" {
  name                = "apim-sbx-${local.s}"
  resource_group_name = azurerm_resource_group.sbx.name
  location            = azurerm_resource_group.sbx.location
  publisher_name      = "AI platform sandbox"
  publisher_email     = var.publisher_email
  sku_name            = "BasicV2_1"

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "apim_models" {
  scope                = azurerm_cognitive_account.models.id
  role_definition_name = "Foundry User"
  principal_id         = azurerm_api_management.gw.identity[0].principal_id
}

output "environment_file" {
  description = "Paste into platform/environments/sandbox.yaml"
  value = yamlencode({
    environment = "sandbox"
    tenant_id   = data.azurerm_client_config.me.tenant_id
    gateway = {
      subscription_id = var.subscription_id
      resource_group  = azurerm_resource_group.sbx.name
      apim_name       = azurerm_api_management.gw.name
      models_product  = "platform-models"
    }
    agents_foundry = {
      subscription_id = var.subscription_id
      resource_group  = azurerm_resource_group.sbx.name
      account_name    = azurerm_cognitive_account.agents.name
    }
    terraform_state = {
      resource_group  = azurerm_resource_group.sbx.name
      storage_account = azurerm_storage_account.state.name
      container       = azurerm_storage_container.state.name
    }
  })
}

output "hosting_account" { value = azurerm_cognitive_account.models.name }
output "project_endpoint" { value = "https://${azurerm_cognitive_account.agents.custom_subdomain_name}.services.ai.azure.com/api/projects/models-demo" }

## Pipeline identity for the GitHub environment "sandbox" (OIDC, no secrets)

variable "github_oidc_subject" {
  description = "OIDC subject of the GitHub environment, e.g. repo:<owner>@<id>/<repo>@<id>:environment:sandbox"
  type        = string
}

resource "azurerm_user_assigned_identity" "pipeline" {
  name                = "id-sbx-pipeline"
  resource_group_name = azurerm_resource_group.sbx.name
  location            = azurerm_resource_group.sbx.location
}

resource "azurerm_federated_identity_credential" "github" {
  name                = "github-sandbox"
  resource_group_name = azurerm_resource_group.sbx.name
  parent_id           = azurerm_user_assigned_identity.pipeline.id
  issuer              = "https://token.actions.githubusercontent.com"
  audience            = ["api://AzureADTokenExchange"]
  subject             = var.github_oidc_subject
}

resource "azurerm_role_assignment" "pipeline" {
  for_each = {
    contributor = { scope = azurerm_resource_group.sbx.id, role = "Contributor" }                      # deployments, APIM, connections
    foundry     = { scope = azurerm_cognitive_account.agents.id, role = "Foundry User" }               # agents, evaluations
    models      = { scope = azurerm_cognitive_account.models.id, role = "Foundry User" }               # smoke tests
    state       = { scope = azurerm_storage_account.state.id, role = "Storage Blob Data Contributor" } # Terraform state
  }
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.pipeline.principal_id
}

output "pipeline_client_id" { value = azurerm_user_assigned_identity.pipeline.client_id }
