variable "environment" {
  description = "Environment name (dev, uat, prd): a project deploys to the environments its project.yaml lists."
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Owner project folder name under projects/."
  type        = string
}

variable "tool" {
  description = "Tool folder name under projects/<project>/tools/."
  type        = string
}

variable "projects_dir" {
  description = "Folder holding <project>/project.yaml and <project>/tools/. Empty: the repository's projects/ folder (tests point it at fixtures)."
  type        = string
  default     = ""
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

variable "platform_environments" {
  description = "The stages the platform offers, comma-separated (repository variable PLATFORM_ENVIRONMENTS); a project's environments only restricts them."
  type        = string
  default     = "dev"
}
