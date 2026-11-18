variable "environment" {
  description = "Environment name: reads platform/environments/<environment>.yaml (must match the owner's project.yaml)."
  type        = string
  default     = "test"
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
