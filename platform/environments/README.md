# Environment files

One file per environment (`test.yaml`, `sandbox.yaml`, later `prod.yaml`) with the platform's coordinates: tenant, AI gateway
(APIM), the Foundry account that holds the agent projects, and the Terraform state storage. Every pipeline reads
the file for the GitHub environment it runs in, so the same code deploys to any environment.

| Section | Used by |
|---|---|
| `tenant_id` | gateway policy deployment: named value `gateway-tenant-id`, read by the `model-api-caller` fragment (Entra token check) |
| `gateway` | model platform deployment, gateway policy deployment (here); use-case onboarding, agent deployment (project repository); smoke tests; `gateway.content_safety` (account name, resource group) is the Azure AI Content Safety account the gateway checks prompts with (required by gateway policy deployment) |
| `agents_foundry` | use-case onboarding (project repository): the account its Foundry projects are created in, their resource IDs, the AI Gateway connections, and the agent pipeline's project endpoints |
| `terraform_state` | Terraform backend of every stack (keys are set per stack by the workflows) |

The files are not secret. The pipeline identity (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`)
is set as GitHub environment variables. Model-hosting accounts are not listed here: they are the folders under
`gateway/foundry-accounts/` in the gateway repository, and this repository never needs them.

The gateway repository (ai-gateway) owns these files; this project repository keeps a copy of the environments it
deploys to. Update the copy when the platform changes.
