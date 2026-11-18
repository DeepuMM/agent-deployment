output "foundry_project_endpoint" {
  description = "The use case's Foundry project: where the agent pipeline deploys its agents."
  value       = local.project_endpoint
}

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

output "tools" {
  description = "Tools this project's agents may use (project.yaml tools.use), with their Foundry connection and gateway endpoint."
  value       = { for k, c in local.tool_connections : k => { connection = c.name, endpoint = c.url } }
}
