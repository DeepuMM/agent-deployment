# Model onboarding stack: models in gateway/foundry-accounts/<account>/models/<model>/ are deployed in their
# hosting Foundry account and registered on the AI gateway (APIM). Own state file (key set by the
# model-onboarding workflow, ai-gateway-models-<environment>.tfstate); the APIM instance and the Foundry
# accounts are only read, never changed.
terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.2, < 5.0"
    }
  }

  backend "azurerm" {}
}

provider "azurerm" {
  features {}
  storage_use_azuread = true
  subscription_id     = local.env.gateway.subscription_id
}
