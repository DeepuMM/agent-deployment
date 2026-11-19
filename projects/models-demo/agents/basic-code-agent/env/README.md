# Settings per environment

`<environment>.env` holds this agent's settings for one environment, as `KEY=VALUE` lines (non-secret). agent-deploy
adds them to that environment's agent version as environment variables, on top of `agent.yaml` `environment`.

| File | Used |
|---|---|
| `dev.env` | yes: models-demo deploys to dev |
| `uat.env.sample`, `prd.env.sample` | no: samples. Rename to `uat.env` / `prd.env` when `project.yaml` lists the environment |

The platform's own variables (`PLATFORM_ENVIRONMENT`, `PLATFORM_PROJECT`, `AI_GATEWAY_URL`, `AI_GATEWAY_PROJECT_URL`,
`AI_GATEWAY_AGENTS_SCOPE`, `AZURE_AI_MODEL_DEPLOYMENT_NAME`) are set by the pipeline and can't be set here. Secrets: Key Vault, never these
files. Details: [README: Settings of a hosted agent](../../../../../README.md#settings-of-a-hosted-agent-environment-variables).
