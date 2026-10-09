"""Strict checks for production code, including every bundled Python resource.

Hyphenated distribution directories keep their runtime paths. A temporary package
view gives mypy valid, distinct namespaces; no source or resource is excluded.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    package = root / "src" / "openharness"
    bundled = package / "plugins" / "bundled"
    with TemporaryDirectory(prefix="openharness-types-") as temporary:
        view_root = Path(temporary)
        view = view_root / "openharness/plugins/bundled"
        for source in bundled.rglob("*.py"):
            relative = source.relative_to(bundled)
            target = view.joinpath(*(part.replace("-", "_") for part in relative.parts))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
        env = {
            **os.environ,
            "MYPYPATH": os.pathsep.join(
                (
                    str(view_root),
                    str(root / "src"),
                    str(root / "evals/research_v1"),
                )
            ),
        }
        base = [sys.executable, "-m", "mypy", "--strict", "--explicit-package-bases", *sys.argv[1:]]
        groups = [[str(package), "--exclude", "plugins/bundled"]]
        groups += [
            [str(directory)]
            for directory in sorted(view.rglob("scripts"))
            if directory.is_dir() and list(directory.glob("*.py"))
        ]
        loose = [str(path) for path in sorted(view.rglob("*.py")) if "scripts" not in path.parts]
        if loose:
            groups.append(loose)
        groups.append([str(root / "evals")])
        groups.append([str(Path(__file__)), str(root / "hatch_build.py")])
        failed = False
        for group in groups:
            result = subprocess.run(base + group, cwd=root, env=env, check=False)
            failed |= result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
