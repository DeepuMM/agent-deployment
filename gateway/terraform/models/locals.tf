locals {
  repo_root    = "${path.module}/../../.."
  accounts_dir = "${local.repo_root}/gateway/foundry-accounts"
  env          = yamldecode(file("${local.repo_root}/platform/environments/${var.environment}.yaml"))

  # One entry per hosting account of this environment: the folder name is the Azure resource name.
  accounts = {
    for f in fileset(local.accounts_dir, "*/account.yaml") :
    dirname(f) => yamldecode(file("${local.accounts_dir}/${f}"))
    if yamldecode(file("${local.accounts_dir}/${f}")).environment == var.environment
  }

  # One entry per model: the folder name is the gateway model name, unique across all accounts.
  models = {
    for f in fileset(local.accounts_dir, "*/models/*/model.yaml") :
    basename(dirname(f)) => merge(yamldecode(file("${local.accounts_dir}/${f}")), {
      account = split("/", f)[0]
      # APIM resource names allow no dots: gpt-5.4-mini -> gpt-5-4-mini
      key        = replace(basename(dirname(f)), ".", "-")
      backend_id = replace("model-${split("/", f)[0]}-${yamldecode(file("${local.accounts_dir}/${f}")).deployment.name}", ".", "-")
    })
    if contains(keys(local.accounts), split("/", f)[0])
  }

  # Registry entry per model, stored in APIM (named value model-meta-<key>). The model-access stack and
  # every project pipeline read the registry from APIM: it is the single source of truth.
  registry = {
    for name, m in local.models : name => {
      name         = name
      api          = m.api
      status       = m.status
      format       = m.model.format
      model        = m.model.name
      version      = m.model.version
      account      = m.account
      region       = local.accounts[m.account].region
      deployment   = m.deployment.name
      capacity_tpm = m.deployment.capacity * 1000
      guardrail    = m.guardrail
      backend_id   = m.backend_id
      kill_switch  = "ks-model-${m.key}"
      # Model card summary for the Developer Portal: the first paragraph of docs.md after its title.
      summary = substr(trimspace(try(split("\n\n", trimspace(replace(file("${local.accounts_dir}/${m.account}/models/${name}/${m.docs_file}"), "/^#[^\n]*\n/", "")))[0], "")), 0, 400)
    }
  }

  # Developer Portal: catalog of every model on the product page (markdown).
  catalog = join("\n", concat([
    "Models served by the AI gateway. A project reaches them through its own model API (`models/<project>`); its agents use `platform-models/<model>`.",
    "",
    "**Request a model:** add it to `models.allowed` in your project's `project.yaml`, with tokens per minute and per day; the platform team approves the pull request.",
    "",
    "| Model | Status | Region | Content safety | Capacity (TPM) | Use in an agent |",
    "|---|---|---|---|---|---|",
    ], [for n, r in local.registry : "| **${n}**: ${r.summary} | ${r.status} | ${r.region} | ${r.guardrail} | ${r.capacity_tpm} | `platform-models/${n}` |"]
  ))
}
