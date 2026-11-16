variable "environment" {
  description = "Environment name: reads platform/environments/<environment>.yaml."
  type        = string
  default     = "test"
}

variable "project" {
  description = "Project folder name under projects/ (also the Foundry project name)."
  type        = string
}

variable "inference_api_version" {
  description = "Azure OpenAI API version the agents' connection uses for chat completions."
  type        = string
  default     = "2024-10-21"
}
