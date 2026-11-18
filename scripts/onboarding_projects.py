"""Projects that use-case onboarding must run for, given the files a change touched (one path per line on stdin).

  git diff --name-only <base> HEAD | python scripts/onboarding_projects.py [<base>]

A project is onboarded when its project.yaml changed, or a tool it owns changed (projects/<p>/tools/<tool>/), or a
tool shared with it changed: its shared_with list before (git show <base>:..., when a base is given) and after the
change, so a project that loses a tool also gets its connection removed. Prints project folder names, one per line.
"""
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def shared_with(text):
    try:
        return (yaml.safe_load(text) or {}).get("shared_with") or []
    except yaml.YAMLError:
        return []


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else None
    out = set()
    for path in (line.strip() for line in sys.stdin):
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "projects":
            continue
        if parts[2] == "project.yaml":
            out.add(parts[1])
        elif parts[2] == "tools" and len(parts) >= 5:
            out.add(parts[1])
            tool_yaml = "/".join(parts[:4] + ["tool.yaml"])
            if os.path.isfile(os.path.join(ROOT, tool_yaml)):
                with open(os.path.join(ROOT, tool_yaml), encoding="utf-8") as fh:
                    out.update(shared_with(fh.read()))
            if base:
                old = subprocess.run(["git", "show", f"{base}:{tool_yaml}"], capture_output=True, text=True, cwd=ROOT)
                if old.returncode == 0:
                    out.update(shared_with(old.stdout))
    for p in sorted(out):
        if os.path.isfile(os.path.join(ROOT, "projects", p, "project.yaml")):
            print(p)


if __name__ == "__main__":
    main()
