"""Model-aware text counting with an explicit conservative fallback."""
from __future__ import annotations

from functools import lru_cache
import logging

log = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _encoding(model: str):
    if not model:
        return None
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model)
    except (ImportError, KeyError, ValueError, OSError):
        return None
    except Exception:
        # An unavailable tokenizer download must not disable budgeting.
        log.warning("Tokenizer unavailable for %s; using conservative estimate", model)
        return None


def counting_method(model: str = "") -> str:
    encoding = _encoding(model)
    return f"tiktoken:{encoding.name}+protocol-estimate" if encoding else "conservative-estimate"


def estimate_tokens(text: str, model: str = "") -> int:
    if not text:
        return 0
    encoding = _encoding(model)
    if encoding is not None:
        return len(encoding.encode(text, disallowed_special=()))
    ascii_count = sum(ord(char) < 128 for char in text)
    return (ascii_count + 2) // 3 + len(text.encode("utf-8")) - ascii_count


def estimate_message_tokens(messages: list[str], model: str = "") -> int:
    return sum(estimate_tokens(message, model) for message in messages)
