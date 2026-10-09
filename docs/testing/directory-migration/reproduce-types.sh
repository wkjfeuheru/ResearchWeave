#!/usr/bin/env bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
source = subprocess.check_output(['git', 'show', '5a5643427def5b748228a975043824fbd5980aba:scripts/check_types.py'], text=True)
source = source.replace('openharness', 'researchx').replace(
    'groups.append([str(Path(__file__)), str(root / "hatch_build.py")])',
    'groups.append([str(root / "hatch_build.py")])',
)
exec(compile(source, 'migration-type-check', 'exec'), {
    '__name__': '__main__', '__file__': str(Path.cwd() / 'scripts/check_types.py'),
})
PY
