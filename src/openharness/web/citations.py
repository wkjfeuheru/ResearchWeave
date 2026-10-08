"""Public website citations projected from an answer's immutable evidence snapshot."""

import re
from urllib.parse import urlsplit

from openharness.research.store import CITATION_PATTERN


def render_web_answer(answer: dict) -> str:
    original = answer["rendered"]
    cited = answer.get("citations", {})
    body, separator, footer = original.rpartition("\n\n来源：")
    notes = dict(re.findall(r"^\[(\d+)\] (.*)$", footer, re.M)) if separator else {}
    text = answer.get("model_text")
    if not isinstance(text, str):
        if not separator or not cited:
            return original
        aliases = {str(item["number"]): key for key, item in cited.items()}
        text = re.sub(r"\[(\d+)\](?!\()", lambda match:
                      f"[E:{aliases[match[1]]}]" if match[1] in aliases else match[0], body)
    visible = {}
    for key, item in cited.items():
        source = item["source"]
        try:
            url = urlsplit(source["locator"])
            is_web = (
                source["kind"] in {"web", "search"} and url.scheme in {"http", "https"}
                and bool(url.hostname) and not any(char.isspace() for char in source["locator"])
                and (url.port is None or 0 < url.port <= 65535)
            )
        except ValueError:
            is_web = False
        if is_web:
            note = notes.get(str(item["number"]))
            if note is None:
                # An incomplete legacy snapshot is not a basis for guessing.
                return original
            visible[key] = note
    numbers = {}

    def replace(match):
        key = match[1]
        if key not in cited:
            return "[来源不可核验]"
        if key not in visible:
            return ""
        numbers.setdefault(key, len(numbers) + 1)
        return f"[{numbers[key]}]"

    rendered = CITATION_PATTERN.sub(replace, text)
    if numbers:
        rendered += "\n\n来源：\n\n" + "\n".join(
            f"{number}. {visible[key]}" for key, number in numbers.items()
        )
    return rendered


def project_answer_rows(rows: list[dict], answers: dict[str, dict]) -> list[dict]:
    """Associate old display text only when its frozen answer is unambiguous."""
    by_text: dict[str, list[str]] = {}
    for key, answer in answers.items():
        by_text.setdefault(answer["rendered"], []).append(key)
    result = []
    for row in rows:
        row = dict(row)
        if row.get("role") == "assistant":
            key = row.get("answer_id")
            if key is None:
                candidates = by_text.get(row.get("text", ""), [])
                key = candidates[0] if len(candidates) == 1 else None
            if key in answers:
                row.update(text=render_web_answer(answers[key]), answer_id=key)
        result.append(row)
    return result
