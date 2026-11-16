output "global_kill_switch" {
  description = "Named value that stops every agent and model API on the gateway when not \"active\"."
  value       = azurerm_api_management_named_value.kill_switch_all.name
}

output "policy_fragments" {
  description = "Shared policy fragments deployed on the gateway."
  value       = sort(keys(azurerm_api_management_policy_fragment.shared))
}
