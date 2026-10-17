#!/usr/bin/env python3
"""Validate workflow files structurally (git-independent, stdlib-only).

Guards against the class of edit that once left release.yml without a
`jobs:` key (nested job dicts inside `permissions:`): every workflow must
parse, declare `jobs`, give each job a runner, and keep `permissions`
values scalar.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))


def main() -> int:
    import yaml  # dev extra only; CI installs it via dev deps

    problems = []
    for wf in WORKFLOWS:
        data = yaml.safe_load(wf.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            problems.append(f"{wf.name}: not a mapping")
            continue
        if "jobs" not in data or not isinstance(data["jobs"], dict) or not data["jobs"]:
            problems.append(f"{wf.name}: missing/empty top-level 'jobs'")
            continue
        perms = data.get("permissions")
        if perms is not None:
            if not isinstance(perms, dict) or not all(isinstance(v, str) for v in perms.values()):
                problems.append(f"{wf.name}: permissions values must be scalars")
        for name, job in data["jobs"].items():
            if not isinstance(job, dict) or "runs-on" not in job:
                problems.append(f"{wf.name}: job '{name}' lacks runs-on")
    if problems:
        print("WORKFLOW-CHECK: RED")
        for p in problems:
            print("  -", p)
        return 1
    print(f"WORKFLOW-CHECK: GREEN — {len(WORKFLOWS)} workflow(s) structurally valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
