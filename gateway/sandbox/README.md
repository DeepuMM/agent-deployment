# Sandbox (disposable)

A throw-away environment to test the pipelines end to end, in one resource group (`rg-ai-gateway-sandbox`): a
model-hosting Foundry account (models only), an agent-only Foundry account with project `models-demo`, an APIM
(Basic v2), an Azure AI Content Safety account (gateway prompt checks; APIM's managed identity gets Cognitive Services
User on it) and a storage account for Terraform state. Local state, not shared. The repo projects `models-demo` and
`proj-default` both deploy their agents into the Foundry project `models-demo`.

```bash
cd gateway/sandbox
terraform init
terraform apply -var subscription_id=<sub> -var publisher_email=<you>
terraform output -raw environment_file > ../../platform/environments/sandbox.yaml
# add gateway/foundry-accounts/<hosting_account>/ and projects/models-demo/, then run, in this order:
#   model-platform-deployment, gateway-policy-deployment, model-project-connection, agent-deploy
terraform destroy -var subscription_id=<sub> -var publisher_email=<you>   # removes everything
```
