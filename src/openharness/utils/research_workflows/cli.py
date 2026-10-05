"""Skill-script CLI: parse documents, validate/compute results and export artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from openharness.utils.fs import atomic_write_text
from openharness.utils.network_guard import fetch_public_http_response
from openharness.utils.research_documents import MAX_DOCUMENT_BYTES, document_text, parse_document
from .models import RESULT_TYPES
from .financial import calculate_financial
from .events import normalize_monitor
from .reports import calculate_deep, normalize_digest
from .export import export_result


def main(argv=None, *, skill_kind=None):
    kinds = list(RESULT_TYPES) if skill_kind is None else [skill_kind]
    if any(kind not in RESULT_TYPES for kind in kinds):
        raise ValueError("Unknown research workflow")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    parse = commands.add_parser("parse")
    parse.add_argument("--input", required=True, help="Local PDF/TXT/MD or public HTTP(S) URL")
    parse.add_argument("--output-dir", required=True)
    for kind in kinds:
        command = commands.add_parser(kind)
        command.add_argument(
            "--input",
            required=True,
            help="Model-extracted JSON matching templates/input.schema.json",
        )
        command.add_argument("--output-dir", required=True)
        command.add_argument(
            "--session-dir", default=os.environ.get("OPENHARNESS_RESEARCH_SESSION_DIR")
        )
        command.add_argument("--task-id", default=os.environ.get("OPENHARNESS_RESEARCH_TASK_ID"))
    schema = commands.add_parser("schema")
    schema.add_argument("kind", choices=kinds)
    args = parser.parse_args(argv)
    try:
        if args.action == "schema":
            print(
                json.dumps(
                    RESULT_TYPES[args.kind].model_json_schema(), ensure_ascii=False, indent=2
                )
            )
            return
        directory = Path(args.output_dir).resolve()
        if args.action == "parse":
            directory.mkdir(parents=True, exist_ok=True)
            if args.input.startswith(("http://", "https://")):
                response = asyncio.run(
                    fetch_public_http_response(args.input, timeout=30, max_bytes=MAX_DOCUMENT_BYTES)
                )
                response.raise_for_status()
                suffix = ".pdf" if response.content.startswith(b"%PDF-") else ".txt"
                if "html" in response.headers.get("content-type", ""):
                    raise ValueError("该链接是HTML页面；请用web_fetch读取并定位PDF原文")
                path = directory / ("downloaded" + suffix)
                path.write_bytes(response.content)
            else:
                path = Path(args.input)
            parsed = parse_document(path)
            if args.input.startswith(("http://", "https://")):
                parsed["source_url"] = str(response.url)
            atomic_write_text(directory / "parsed.json", json.dumps(parsed, ensure_ascii=False))
            atomic_write_text(directory / "text.md", document_text(parsed))
            print(
                json.dumps(
                    {
                        "status": parsed["status"],
                        "gaps": parsed["gaps"],
                        "index": str(directory / "parsed.json"),
                        "text": str(directory / "text.md"),
                    },
                    ensure_ascii=False,
                )
            )
            return
        result = RESULT_TYPES[args.action].model_validate_json(Path(args.input).read_text())
        function = {
            "financial": calculate_financial,
            "monitor": normalize_monitor,
            "digest": normalize_digest,
            "deep": calculate_deep,
        }[args.action]
        result = function(result)
        exported = export_result(
            result, directory, Path(args.session_dir) if args.session_dir else None, args.task_id
        )
        print(json.dumps(exported, ensure_ascii=False))
    except Exception as exc:
        # CLI errors are useful to the model, but never dump request headers or secrets.
        from openharness.utils.redaction import memory_credentials

        message = str(exc)
        for secret in memory_credentials():
            if secret:
                message = message.replace(secret, "[hidden]")
        print(
            json.dumps({"status": "failed", "error": message[:4000]}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
