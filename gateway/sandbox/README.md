# Sandbox (disposable)

A throw-away environment to test the model pipelines end to end, in one resource group
(`rg-ai-gateway-sandbox`): a model-hosting Foundry account, an agent-only Foundry account with project
`models-demo`, an APIM (Basic v2) and a storage account for Terraform state. Local state, not shared.

```bash
cd gateway/sandbox
terraform init
terraform apply -var subscription_id=<sub> -var publisher_email=<you>
terraform output -raw environment_file > ../../platform/environments/sandbox.yaml
# add gateway/foundry-accounts/<hosting_account>/ and projects/models-demo/, then run the two stacks
terraform destroy -var subscription_id=<sub> -var publisher_email=<you>   # removes everything
```
