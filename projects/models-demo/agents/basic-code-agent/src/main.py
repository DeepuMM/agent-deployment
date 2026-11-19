# Basic hosted (code) agent, from the Microsoft Foundry sample
# samples/python/hosted-agents/agent-framework/responses/01-basic (Copyright (c) Microsoft).
# Platform changes:
# - the model is the project's gateway connection ("<connection>/<model>", set by the pipeline in
#   AZURE_AI_MODEL_DEPLOYMENT_NAME from agent.yaml `model`), so every model call goes through the AI gateway;
# - two tools to call other agents through the gateway (agents calling agents, README: Access to agents):
#   ask_agent (only the agents listed in CALLABLE_AGENTS, env/<environment>.env) and gateway_identity (the identity
#   this agent calls with: the one the called agent's role must be granted to).

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Annotated

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# Local runs only: Foundry sets FOUNDRY_PROJECT_ENDPOINT in the hosted container.
load_dotenv()

log = logging.getLogger(__name__)
credential = DefaultAzureCredential()
# Agents this agent may ask, "<project>/<agent>" (prompt agents: their gateway path is ai/<project>/<agent>/).
CALLABLE_AGENTS = sorted({x.strip() for x in os.getenv("CALLABLE_AGENTS", "").split(",") if x.strip()})

INSTRUCTIONS = (
    "You are a friendly assistant. Keep your answers brief. "
    + (f"When a question belongs to one of these agents, ask it with the ask_agent tool and answer from its reply: "
       f"{', '.join(CALLABLE_AGENTS)} (proj-default/leave-policy-agent: leave, holidays and sick days). "
       if CALLABLE_AGENTS else "")
    + "When asked which identity you call other agents with, use the gateway_identity tool."
)


def ask_agent(
    agent: Annotated[str, "The agent to ask, as <project>/<agent>, e.g. proj-default/leave-policy-agent."],
    question: Annotated[str, "The question for that agent, complete and self-contained."],
) -> str:
    """Ask another agent of the platform through the AI gateway and return its answer."""
    if agent not in CALLABLE_AGENTS:
        return f"Not allowed: this agent may only ask {', '.join(CALLABLE_AGENTS) or 'no other agent'}."
    project, name = agent.split("/", 1)
    try:   # this agent's own identity, a token for the gateway's agent app (AI_GATEWAY_AGENTS_SCOPE, set by the pipeline)
        token = credential.get_token(os.environ["AI_GATEWAY_AGENTS_SCOPE"]).token
    except Exception as e:   # e.g. AADSTS501051: the identity holds no role on the agent app yet
        return f"Could not get a token for the AI gateway's agent app (no role granted to this agent's identity?): {e}"[:600]
    request = urllib.request.Request(
        f"{os.environ['AI_GATEWAY_URL']}/ai/{project}/{name}/openai/v1/responses", method="POST",
        data=json.dumps({"input": question}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:   # 403 AGENT_NOT_ALLOWED: the role isn't granted (yet)
        return f"The gateway refused the call: HTTP {e.code} {e.read()[:400].decode(errors='replace')}"
    answer = " ".join(c.get("text", "") for item in body.get("output", []) if item.get("type") == "message"
                      for c in item.get("content", []))
    log.info("ask_agent %s: %d characters", agent, len(answer))
    return answer or json.dumps(body)[:600]


def gateway_identity() -> str:
    """The identity this agent uses to call other agents through the AI gateway (object ID and kind)."""
    token = credential.get_token("https://ai.azure.com/.default").token
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return json.dumps({k: claims.get(k) for k in ("oid", "appid", "xms_mirid", "idtyp") if claims.get(k)})


def main():
    # Settings come from environment variables: agent.yaml `environment`, env/<environment>.env, and the platform's
    # (PLATFORM_ENVIRONMENT, PLATFORM_PROJECT, AI_GATEWAY_URL, AI_GATEWAY_PROJECT_URL, AI_GATEWAY_AGENTS_SCOPE,
    # AZURE_AI_MODEL_DEPLOYMENT_NAME).
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    log.info("starting in %s (project %s, gateway %s, may ask %s)", os.getenv("PLATFORM_ENVIRONMENT", "local"),
             os.getenv("PLATFORM_PROJECT", "-"), os.getenv("AI_GATEWAY_URL", "-"), CALLABLE_AGENTS or "no agent")
    model_name = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    if not model_name:
        raise RuntimeError("AZURE_AI_MODEL_DEPLOYMENT_NAME is not set (the pipeline sets it from agent.yaml `model`).")

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=model_name,
        credential=credential,
    )

    agent = Agent(
        client=client,
        instructions=INSTRUCTIONS,
        tools=[ask_agent, gateway_identity],
        # History is managed by the hosting infrastructure, so the service doesn't store it.
        default_options={"store": False},
    )

    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
