# Tool deployment stack: one run per project-owned tool (projects/<project>/tools/<tool>/tool.yaml). Publishes the
# tool on the AI gateway: its API tools/<project>/<tool>/mcp, the policy with its auth guard (the Foundry projects in
# shared_with plus the owner's), its kill switch and its registry entry tool-meta-<project>-<tool>. Consumers, in this
# repository or any other, connect to it through their own use-case onboarding, which reads the registry entry.
# Own state file per tool (key set by the tool-deploy workflow: tool-<project>-<tool>-<environment>.tfstate).
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
