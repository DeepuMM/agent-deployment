"""What one pipeline must deploy, per environment, for its workflow's matrices.

  pipeline_plan.py <tools|onboarding|agents> [--all] [--base <sha>]

Prints key=value lines for $GITHUB_OUTPUT: dev, uat, prd, each a JSON list for strategy.matrix.include, and any
(true when something is to be deployed). An entry is {"project": <p>} (onboarding, agents) or {"project": <p>,
"tool": <t>} (tools), and appears under an environment only if the project's project.yaml lists it.

What counts as changed since <base> (--all, or no usable base: everything):
  tools       projects/<p>/tools/<t>/**; everything when tool-deploy/ or scripts/tool_smoke.py changed
  onboarding  projects/<p>/project.yaml; everything when onboarding/ changed
  agents      projects/<p>/** outside tools/; everything when templates/ changed
"""
import glob
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENVIRONMENTS = ("dev", "uat", "prd")
SHARED = {"tools": ("tool-deploy/", "scripts/tool_smoke.py"), "onboarding": ("onboarding/",), "agents": ("templates/",)}


def projects():
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "projects", "*", "project.yaml"))):
        with open(f, encoding="utf-8") as fh:
            out[os.path.basename(os.path.dirname(f))] = (yaml.safe_load(fh) or {}).get("environments") or ["dev"]
    return out


def changed(base):
    if not base or set(base) == {"0"}:
        return None
    r = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], capture_output=True, text=True, cwd=ROOT)
    return None if r.returncode else [x for x in r.stdout.splitlines() if x]


def entries(kind, files, envs):
    everything = files is None or any(f.startswith(SHARED[kind]) for f in files)
    if kind == "tools":
        tools = sorted({(f.split("/")[1], f.split("/")[3]) for f in glob.glob("projects/*/tools/*/tool.yaml", root_dir=ROOT)})
        if not everything:
            touched = {(f.split("/")[1], f.split("/")[3]) for f in files
                       if f.startswith("projects/") and len(f.split("/")) > 4 and f.split("/")[2] == "tools"}
            tools = [t for t in tools if t in touched]
        return [{"project": p, "tool": t} for p, t in tools if p in envs]
    if kind == "onboarding":
        names = [p for p in envs if everything or f"projects/{p}/project.yaml" in files]
    else:
        names = [p for p in envs if everything or any(
            f.startswith(f"projects/{p}/") and not f.startswith(f"projects/{p}/tools/") for f in files)]
    return [{"project": p} for p in names]


def main(argv):
    kind = argv[0]
    base = argv[argv.index("--base") + 1] if "--base" in argv and argv.index("--base") + 1 < len(argv) else ""
    envs = projects()
    items = entries(kind, None if "--all" in argv else changed(base), envs)
    for e in ENVIRONMENTS:
        print(f"{e}={json.dumps([i for i in items if e in envs[i['project']]])}")
    print(f"any={str(bool(items)).lower()}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("tools", "onboarding", "agents"):
        sys.exit(__doc__)
    main(sys.argv[1:])
