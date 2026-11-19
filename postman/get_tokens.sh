#!/usr/bin/env bash
# Writes postman/local.postman_environment.json (git-ignored): the dev template plus your two tokens, from the Azure
# CLI as the signed-in user. Import it in Postman next to agent-access.postman_collection.json. Tokens last about an
# hour: run again, re-import (or paste the values) when calls start returning 401.
#
#   postman/get_tokens.sh [AGENTS_APP_ID]      (default: GitHub variable AGENTS_APP_ID of environment agent-dev)
#
#   agent_app_token  scope <AGENTS_APP_ID>/.default    the gateway's agent app; carries your agent roles. Fails with
#                    AADSTS50105 until you hold at least one of its roles (grant_demo_access.sh).
#   foundry_token    scope https://ai.azure.com/.default  for agents whose access lists foundry
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
app=${1:-$(gh variable get AGENTS_APP_ID --env agent-dev 2>/dev/null || true)}
[ -n "$app" ] || { echo "usage: $0 <AGENTS_APP_ID> (or gh access to read it from agent-dev)" >&2; exit 1; }
agent_app_token=$(az account get-access-token --scope "$app/.default" --query accessToken -o tsv)
foundry_token=$(az account get-access-token --scope https://ai.azure.com/.default --query accessToken -o tsv)
python3 - "$here" "$agent_app_token" "$foundry_token" <<'EOF'
import json, sys, base64
here, app_token, foundry_token = sys.argv[1:]
env = json.load(open(f"{here}/dev.postman_environment.json"))
env["name"], env["id"] = "AI gateway dev (local tokens)", env["id"][:-1] + "1"
for v in env["values"]:
    v["value"] = {"agent_app_token": app_token, "foundry_token": foundry_token}.get(v["key"], v["value"])
json.dump(env, open(f"{here}/local.postman_environment.json", "w"), indent=2)
p = app_token.split(".")[1]
roles = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("roles", [])
print(f"wrote {here}/local.postman_environment.json; your agent roles: {', '.join(roles) or 'none'}")
EOF
