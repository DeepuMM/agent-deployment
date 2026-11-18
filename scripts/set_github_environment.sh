#!/usr/bin/env bash
# Sets an environment's coordinates as GitHub environment variables, on both of its GitHub environments:
# model-<env> (tools, onboarding) and agent-<env> (agents). The pipelines read only these; nothing about an environment
# is kept in the repository. Values are not secret. Run by a platform engineer when an environment is created or
# changes (needs admin on the repository: gh auth login).
#
#   scripts/set_github_environment.sh <env> KEY=VALUE ...
#
# Keys (all required unless marked):
#   AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID      pipeline sign-in (AZURE_CLIENT_ID is set per GitHub environment, with
#                                               the identity's federated credential for that environment)
#   APIM_SUBSCRIPTION_ID APIM_RESOURCE_GROUP APIM_NAME [MODELS_PRODUCT=platform-models]
#   FOUNDRY_SUBSCRIPTION_ID FOUNDRY_RESOURCE_GROUP FOUNDRY_ACCOUNT
#   TF_STATE_RESOURCE_GROUP TF_STATE_STORAGE_ACCOUNT TF_STATE_CONTAINER
#   [PORTAL_ANONYMOUS=false]
set -euo pipefail
env_name=${1:?usage: set_github_environment.sh <env> KEY=VALUE ...}; shift
required=(AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID APIM_SUBSCRIPTION_ID APIM_RESOURCE_GROUP APIM_NAME
          FOUNDRY_SUBSCRIPTION_ID FOUNDRY_RESOURCE_GROUP FOUNDRY_ACCOUNT
          TF_STATE_RESOURCE_GROUP TF_STATE_STORAGE_ACCOUNT TF_STATE_CONTAINER)
declare -A values=([MODELS_PRODUCT]=platform-models [PORTAL_ANONYMOUS]=false)
for kv in "$@"; do values[${kv%%=*}]=${kv#*=}; done
for k in "${required[@]}"; do [ -n "${values[$k]:-}" ] || { echo "missing $k" >&2; exit 1; }; done
for gh_env in "model-$env_name" "agent-$env_name"; do
  # Create the GitHub environment only if it's missing: updating an existing one could reset its protection rules.
  gh api "repos/{owner}/{repo}/environments/$gh_env" --silent 2>/dev/null \
    || gh api -X PUT "repos/{owner}/{repo}/environments/$gh_env" --silent
  for k in "${!values[@]}"; do
    gh variable set "$k" --env "$gh_env" --body "${values[$k]}"
  done
  echo "$gh_env: ${#values[@]} variables set"
done
