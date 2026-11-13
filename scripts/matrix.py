"""Builds GitHub Actions job matrices: which platform environment (and GitHub environment) each run targets.

  matrix.py accounts [<account> ...]            model onboarding: one entry per environment of those hosting
                                                accounts (none given: every environment that has accounts)
  matrix.py projects <kind> <project> ...       model access / agent deploy: one entry per project, with the
                                                environment from its project.yaml (default "test")

GitHub environment names: "test" keeps the original names (model-test, agent-test); any other platform
environment uses its own name (e.g. sandbox), whose variables point at that environment's Azure resources.
MODEL_ENVIRONMENTS (comma-separated, e.g. "sandbox,test"), when set, limits the model pipelines (accounts, and
projects of kind "model") to environments whose GitHub environment and Azure identity are set up.
Prints a JSON list for `strategy.matrix.include`.
"""
import glob
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def github_env(platform_env, kind):
    return f"{kind}-test" if platform_env == "test" else platform_env


def main(args):
    if args[0] == "accounts":
        names = args[1:] or [os.path.basename(os.path.dirname(f))
                             for f in glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", "*", "account.yaml"))]
        envs = sorted({yaml.safe_load(open(os.path.join(ROOT, "gateway", "foundry-accounts", a, "account.yaml")))["environment"]
                       for a in names if os.path.exists(os.path.join(ROOT, "gateway", "foundry-accounts", a, "account.yaml"))})
        return [{"environment": e, "github_environment": github_env(e, "model")} for e in envs]
    kind, projects = args[1], args[2:]
    out = []
    for p in projects:
        f = os.path.join(ROOT, "projects", p, "project.yaml")
        if os.path.exists(f):
            e = (yaml.safe_load(open(f)) or {}).get("environment", "test")
            out.append({"project": p, "environment": e, "github_environment": github_env(e, kind)})
    return out


def enabled(entries, kind):
    allowed = [e.strip() for e in os.environ.get("MODEL_ENVIRONMENTS", "").split(",") if e.strip()]
    if kind == "agent" or not allowed:
        return entries
    return [e for e in entries if e["environment"] in allowed]


if __name__ == "__main__":
    kind = "model" if sys.argv[1] == "accounts" else sys.argv[2]
    print(json.dumps(enabled(main(sys.argv[1:]), kind)))
