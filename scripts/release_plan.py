"""What a project release must do, for the release workflow (.github/workflows/_release.yml).

  release_plan.py <project> [--all] [--base <sha>]

Prints key=value lines for $GITHUB_OUTPUT:
  environments  JSON list from project.yaml environments (promotion order; default ["test"])
  tools         JSON list of the project's tools to deploy: every tool with --all, else those changed since <base>
  project       true when use-case onboarding must run: --all, or project.yaml changed
  agents        true when the agents must be released: --all, or anything outside tools/ changed

--all (manual runs, release tags, platform changes) or no usable base: everything. A deleted tool has nothing to
deploy and is left out.
"""
import glob
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def changed_files(project, base):
    if not base or set(base) == {"0"}:
        return None
    r = subprocess.run(["git", "diff", "--name-only", base, "HEAD", "--", f"projects/{project}/"],
                       capture_output=True, text=True, cwd=ROOT)
    return None if r.returncode else [line for line in r.stdout.splitlines() if line]


def main(argv):
    project = argv[0]
    everything = "--all" in argv
    base = argv[argv.index("--base") + 1] if "--base" in argv and argv.index("--base") + 1 < len(argv) else ""
    folder = os.path.join(ROOT, "projects", project)
    with open(os.path.join(folder, "project.yaml"), encoding="utf-8") as fh:
        p = yaml.safe_load(fh) or {}
    all_tools = sorted(os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(folder, "tools", "*", "tool.yaml")))
    files = None if everything else changed_files(project, base)
    if files is None:
        tools, onboard, agents = all_tools, True, True
    else:
        rel = [f.split("/", 2)[2] for f in files]          # path inside the project folder
        tools = sorted({r.split("/")[1] for r in rel if r.startswith("tools/") and len(r.split("/")) > 2} & set(all_tools))
        onboard = "project.yaml" in rel
        agents = any(not r.startswith("tools/") for r in rel)
    out = {"environments": json.dumps(p.get("environments") or ["test"]), "tools": json.dumps(tools),
           "project": str(onboard).lower(), "agents": str(agents).lower()}
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    if not sys.argv[1:]:
        sys.exit(__doc__)
    main(sys.argv[1:])
