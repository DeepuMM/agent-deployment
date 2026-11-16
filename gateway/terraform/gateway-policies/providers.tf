# Gateway policy stack: the shared AI gateway policy fragments in gateway/policy-fragments/ (one APIM policy fragment
# per file) and the named values they read. Every project API, agent operation and project model API policy includes
# them, so the shared logic lives once per APIM. Own state file (key set by the gateway-policy-deployment workflow,
# ai-gateway-policies-<environment>.tfstate); the APIM instance is only read, never changed.
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
