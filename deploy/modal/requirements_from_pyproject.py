"""Print pip requirements for the base dependencies plus selected extras.

Lets the Modal image install third-party packages in a layer that only
rebuilds when pyproject.toml changes, before the source tree is copied in.

    python requirements_from_pyproject.py pyproject.toml dev,models > requirements.txt
"""

from __future__ import annotations

import sys
import tomllib


def main() -> None:
    path = sys.argv[1]
    extras = [e for e in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if e]
    with open(path, "rb") as fh:
        project = tomllib.load(fh)["project"]
    reqs = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"unknown extra: {extra}")
        reqs.extend(optional[extra])
    seen: set[str] = set()
    for req in reqs:
        if req not in seen:
            seen.add(req)
            print(req)


if __name__ == "__main__":
    main()
