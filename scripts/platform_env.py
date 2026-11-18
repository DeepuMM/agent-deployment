"""The platform environment a pipeline job runs against, from its GitHub environment's variables.

Nothing about an environment is kept in this repository: each GitHub environment (model-<env>, agent-<env>) holds
its coordinates as variables, and the workflows pass them to the job's environment. Locally, export the same names
(see README: Run locally).

  APIM_SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_NAME, MODELS_PRODUCT     the AI gateway
  FOUNDRY_SUBSCRIPTION_ID, FOUNDRY_RESOURCE_GROUP, FOUNDRY_ACCOUNT        the agent Foundry account
"""
import os
import sys


def need(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name} (a GitHub environment variable; export it to run locally)")
    return value


def environment():
    """The environment's coordinates, shaped {gateway: {...}, agents_foundry: {...}}."""
    return {
        "gateway": {
            "subscription_id": os.environ.get("APIM_SUBSCRIPTION_ID") or need("AZURE_SUBSCRIPTION_ID"),
            "resource_group": need("APIM_RESOURCE_GROUP"),
            "apim_name": need("APIM_NAME"),
            "models_product": os.environ.get("MODELS_PRODUCT", "platform-models"),
        },
        "agents_foundry": {
            "subscription_id": os.environ.get("FOUNDRY_SUBSCRIPTION_ID") or need("AZURE_SUBSCRIPTION_ID"),
            "resource_group": need("FOUNDRY_RESOURCE_GROUP"),
            "account_name": need("FOUNDRY_ACCOUNT"),
        },
    }
