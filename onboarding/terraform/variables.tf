variable "environment" {
  description = "Environment name (dev, uat, prd): a project deploys to the environments its project.yaml lists."
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project folder name under projects/ (also the Foundry project name)."
  type        = string
}

variable "projects_dir" {
  description = "Folder holding <project>/project.yaml. Empty: the repository's projects/ folder (tests point it at fixtures)."
  type        = string
  default     = ""
}

variable "inference_api_version" {
  description = "Azure OpenAI API version the agents' connection uses for chat completions."
  type        = string
  default     = "2024-10-21"
}

# Platform coordinates of the environment: GitHub environment variables (model-<environment>), passed by the workflow
# as TF_VAR_*. Nothing about an environment is kept in this repository.
variable "apim_subscription_id" {
  description = "Subscription of the AI gateway (APIM). GitHub variable APIM_SUBSCRIPTION_ID."
  type        = string
}

variable "apim_resource_group" {
  description = "Resource group of the AI gateway. GitHub variable APIM_RESOURCE_GROUP."
  type        = string
}

variable "apim_name" {
  description = "The AI gateway (APIM instance). GitHub variable APIM_NAME."
  type        = string
}

variable "models_product" {
  description = "APIM product the project's model and tool APIs are added to. GitHub variable MODELS_PRODUCT."
  type        = string
  default     = "platform-models"
}

variable "foundry_subscription_id" {
  description = "Subscription of the environment's agent Foundry account. GitHub variable FOUNDRY_SUBSCRIPTION_ID."
  type        = string
}

variable "foundry_resource_group" {
  description = "Resource group of the agent Foundry account. GitHub variable FOUNDRY_RESOURCE_GROUP."
  type        = string
}

variable "foundry_account" {
  description = "The agent Foundry account holding the projects. GitHub variable FOUNDRY_ACCOUNT."
  type        = string
}
