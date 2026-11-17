# Basic hosted (code) agent, from the Microsoft Foundry sample
# samples/python/hosted-agents/agent-framework/responses/01-basic (Copyright (c) Microsoft).
# Platform change: the model is the project's gateway connection ("<connection>/<model>", set by the pipeline in
# AZURE_AI_MODEL_DEPLOYMENT_NAME from agent.yaml `model`), so every model call goes through the AI gateway.

import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# Local runs only: Foundry sets FOUNDRY_PROJECT_ENDPOINT in the hosted container.
load_dotenv()

INSTRUCTIONS = "You are a friendly assistant. Keep your answers brief."


def main():
    model_name = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    if not model_name:
        raise RuntimeError("AZURE_AI_MODEL_DEPLOYMENT_NAME is not set (the pipeline sets it from agent.yaml `model`).")

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=model_name,
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        instructions=INSTRUCTIONS,
        # History is managed by the hosting infrastructure, so the service doesn't store it.
        default_options={"store": False},
    )

    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
