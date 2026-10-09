"""Stream CI output and expose bounded diagnostics through check annotations.

GitHub's public job metadata is readable without an admin token, while its
download-logs endpoint is not. Keep the command's exit status and test selection.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import deque


def annotate(kind: str, message: str) -> None:
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)[-16000:]
    # GitHub truncates one annotation around 4 KiB. Preserve the complete
    # bounded tail through smaller chunks, including the final failure summary.
    for offset in range(0, len(message), 600):
        chunk = message[offset : offset + 600]
        chunk = chunk.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{kind} title=Validation result::{chunk}", flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: ci_validation.py COMMAND [ARG ...]")
    tail: deque[str] = deque(maxlen=200)
    with subprocess.Popen(
        sys.argv[1:],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as process:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                tail.append(line[-16000:])
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if os.environ.get("GITHUB_ACTIONS") == "true":
        summary = [line for line in tail if re.search(r"\d+ passed|PASS:", line)]
        annotate("error" if code else "notice", "".join(tail if code else summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
