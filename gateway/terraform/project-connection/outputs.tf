output "connection" {
  description = "Agents in this project use \"<connection>/<model>\"."
  value       = local.connection
}

output "models" {
  description = "Models this project can use, with its limits."
  value       = { for n, m in local.models : n => { use_as = "${local.connection}/${n}", tokens_per_minute = m.tokens_per_minute, tokens_per_day = m.tokens_per_day } }
}

output "endpoint" {
  description = "The project's model API on the gateway (called by Foundry with the project's identity)."
  value       = "${data.azurerm_api_management.this.gateway_url}/models/${var.project}"
}
