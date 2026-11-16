output "policy_fragments" {
  description = "Shared policy fragments deployed on the gateway."
  value       = sort(keys(azurerm_api_management_policy_fragment.shared))
}
