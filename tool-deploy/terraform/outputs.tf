output "endpoint" {
  description = "The tool's MCP endpoint on the gateway (consumers' connections point here)."
  value       = local.endpoint
}

output "foundry_projects" {
  description = "Foundry projects whose agents may call the tool (the auth guard)."
  value       = concat([local.owner_foundry_project], local.shared_with)
}

output "kill_switch" {
  description = "Named value that suspends the tool."
  value       = local.kill_switch
}
