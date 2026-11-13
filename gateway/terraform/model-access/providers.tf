# Model access stack: one run per project. Gives projects/<project>/ access to the platform models it lists in
# project.yaml (models.allowed): its own model API on the AI gateway with per-model token limits, and an
# AI Gateway connection in its Foundry project so its agents use "<connection>/<model>".
# Own state file per project (key set by the model-access workflow:
# ai-gateway-model-access-<project>-<environment>.tfstate). The model registry is READ from APIM
# (named values model-meta-*), never from Git: what the gateway serves is the single source of truth.
terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.2, < 5.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.4"
    }
  }

  backend "azurerm" {}
}

provider "azurerm" {
  features {}
  storage_use_azuread = true
  subscription_id     = local.env.gateway.subscription_id
}

provider "azapi" {}
