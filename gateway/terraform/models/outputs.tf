output "registry" {
  description = "Models registered on the gateway (also stored in APIM as model-meta-<model>)."
  value       = local.registry
}

output "gateway_url" {
  description = "AI gateway base URL."
  value       = data.azurerm_api_management.this.gateway_url
}
