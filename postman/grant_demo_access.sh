#!/usr/bin/env bash
# Access for the Postman demo in dev, granted OUTSIDE the pipelines (README: Access to agents). Run by a person who
# may assign the agent app's roles (an owner of the enterprise app AI Gateway Agents (dev), or an admin) and Foundry
# roles on the agent account (Owner or Role Based Access Control Administrator). Idempotent. Run it after agent-deploy
# has released models-demo and proj-default with access lists (the agent roles exist from then on).
#
#   postman/grant_demo_access.sh                       you: the roles for folders 1-3 of the collection
#   postman/grant_demo_access.sh <basic-code-agent oid>  + agent to agent: that identity may call leave-policy-agent
#                                                        (the object ID from request 5.1)
#
# Grants (you = the signed-in user):
#   you              app role agent.models-demo.basic-code-agent    folder 1 ([gateway])
#   you              app role agent.models-demo.docs-assistant      folder 3 ([gateway, foundry])
#   you              Foundry Agent Consumer on project models-demo  folders 2 and 3 (foundry)
#   <oid>            app role agent.proj-default.leave-policy-agent folder 5 (agent to agent)
#   docs-assistant's identity  app role tool.models-demo.ms-learn  its tool calls (access per agent)
#   tool-smoke's identity      app role tool.models-demo.all       tool-deploy's tool tests (once tool-deploy created it)
# Nothing for proj-default to you: folder 4 shows the refusal (403 AGENT_NOT_ALLOWED).
set -euo pipefail
APP=${AGENTS_APP_ID:-$(gh variable get AGENTS_APP_ID --env agent-dev)}
FOUNDRY_RG=${FOUNDRY_RESOURCE_GROUP:-$(gh variable get FOUNDRY_RESOURCE_GROUP --env agent-dev)}
FOUNDRY_ACCOUNT=${FOUNDRY_ACCOUNT:-$(gh variable get FOUNDRY_ACCOUNT --env agent-dev)}
GRAPH=https://graph.microsoft.com/v1.0
SP=$(az ad sp show --id "$APP" --query id -o tsv)

grant_app_role() {   # <principal object ID> <role value> <label>
  local role
  role=$(az ad sp show --id "$APP" --query "appRoles[?value=='$2'].id" -o tsv)
  [ -n "$role" ] || { echo "role $2 does not exist yet: run python3 scripts/agent_pipeline.py roles (as an owner of the agent app)" >&2; exit 1; }
  if az rest --url "$GRAPH/servicePrincipals/$SP/appRoleAssignedTo" --query "value[?principalId=='$1' && appRoleId=='$role'].id" -o tsv | grep -q .; then
    echo "  $3: $2 (already)"
  else
    az rest --method POST --url "$GRAPH/servicePrincipals/$SP/appRoleAssignedTo" -o none \
      --body "{\"principalId\":\"$1\",\"resourceId\":\"$SP\",\"appRoleId\":\"$role\"}"
    echo "  $3: $2"
  fi
}

ME=$(az ad signed-in-user show --query id -o tsv)
echo "== you ($ME)"
grant_app_role "$ME" agent.models-demo.basic-code-agent you
grant_app_role "$ME" agent.models-demo.docs-assistant you
FOUNDRY_SUB=${FOUNDRY_SUBSCRIPTION_ID:-$(gh variable get FOUNDRY_SUBSCRIPTION_ID --env agent-dev)}
PROJECT_ID="/subscriptions/$FOUNDRY_SUB/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT/projects/models-demo"
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User --role "Foundry Agent Consumer" \
  --scope "$PROJECT_ID" -o none && echo "  you: Foundry Agent Consumer on $FOUNDRY_ACCOUNT/models-demo"

if [ -n "${1:-}" ]; then
  echo "== basic-code-agent's identity ($1)"
  grant_app_role "$1" agent.proj-default.leave-policy-agent basic-code-agent
fi
# Tools: access is per agent. Each agent's own identity holds the tool's role (README: Tools).
agent_identity() {   # <Foundry project> <agent>: object ID of the agent's Entra agent identity, empty if none yet
  az ad sp list --display-name "$FOUNDRY_ACCOUNT-$1-$2-AgentIdentity" --query "[0].id" -o tsv
}
echo "== tools (models-demo)"
for pair in "docs-assistant tool.models-demo.ms-learn" "tool-smoke tool.models-demo.all"; do
  set -- $pair
  OID=$(agent_identity models-demo "$1")
  if [ -n "$OID" ]; then grant_app_role "$OID" "$2" "$1"; else echo "  $1: no agent identity yet (deploy it first)"; fi
done
echo "Get new tokens (postman/get_tokens.sh): roles are in tokens issued from now on."
