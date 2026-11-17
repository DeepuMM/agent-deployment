locals {
  repo_root = "${path.module}/../../.."
  env       = yamldecode(file("${local.repo_root}/platform/environments/${var.environment}.yaml"))

  # Azure AI Content Safety account the model-content-safety fragment calls (required in every environment file).
  content_safety_set = can(local.env.gateway.content_safety.account_name) && can(local.env.gateway.content_safety.resource_group)
  content_safety     = local.content_safety_set ? local.env.gateway.content_safety : { account_name = "not-set", resource_group = "not-set" }

  # gateway/policy-fragments/<id>.xml -> APIM policy fragment <id>.
  fragments_dir = "${local.repo_root}/gateway/policy-fragments"
  policy_fragments = {
    for f in fileset(local.fragments_dir, "*.xml") : trimsuffix(f, ".xml") => file("${local.fragments_dir}/${f}")
  }

  # Every policy that includes fragments: the agent pipeline's templates and the project model API policy. Each
  # fragment they include must have a file here.
  policy_templates = concat(
    [for f in fileset("${local.repo_root}/templates", "*.xml") : "${local.repo_root}/templates/${f}"],
    [for f in fileset("${local.repo_root}/gateway/terraform/project-connection/policies", "*.tftpl") : "${local.repo_root}/gateway/terraform/project-connection/policies/${f}"],
  )
  referenced_fragments = distinct(flatten([for f in local.policy_templates : regexall("fragment-id=\"([^\"]+)\"", file(f))]))
}
