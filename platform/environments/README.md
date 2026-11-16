# Environment files

One file per environment (`test.yaml`, later `prod.yaml`) with the platform's coordinates: tenant, AI gateway
(APIM), the Foundry account that holds the agent projects, and the Terraform state storage. Every pipeline reads
the file for the GitHub environment it runs in, so the same code deploys to any environment.

| Section | Used by |
|---|---|
| `tenant_id` | gateway policy deployment: named value `gateway-tenant-id`, read by the `model-api-caller` fragment (Entra token check) |
| `gateway` | model platform deployment, gateway policy deployment, model project connection, agent deployment, smoke tests |
| `agents_foundry` | model project connection: the project's resource ID and the AI Gateway connection |
| `terraform_state` | Terraform backend of every stack (keys are set per stack by the workflows) |

The files are not secret. The pipeline identity (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`)
is set as GitHub environment variables. Model-hosting accounts are not listed here: they are the folders under
`gateway/foundry-accounts/`, and project repositories never need them.

In a use-case repository created from the template, use-case onboarding writes this file from what it provisioned.
Don't edit it by hand.
