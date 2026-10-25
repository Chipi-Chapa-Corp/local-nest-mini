"""Voice assistant replies from a configured OpenAI-compatible Chat API."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


SYSTEM_PROMPT = """# You
You are a voice assistant named Laylo (pronounced "lejlo"). Respond quickly, briefly, and write only text suitable for speaking aloud.

# Responding
- Output only ordinary spoken text. Do not use tags in square brackets, Markdown, stage directions, or other formatting.
- Prefer concise responses in one or a few sentences.
- Always respond entirely in the language of the request. Do not mix spoken languages.
- Write numbers and abbreviations as they should be spoken in that language.
- Speak as a human would: naturally and realistically.
"""

LLM_TIMEOUT = 300


def respond(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("input must not be empty")
    base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "").strip()
    if not base_url or not model:
        raise RuntimeError("LLM_BASE_URL and LLM_MODEL must be configured")
    url = urlsplit(base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.query or url.fragment:
        raise RuntimeError("LLM_BASE_URL must be an HTTP(S) API base URL, e.g. http://localhost:8000/v1")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    reasoning = os.environ.get("LLM_REASONING_EFFORT", "").strip()
    if reasoning:
        payload["reasoning_effort"] = reasoning
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(base_url + "/chat/completions",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
    try:
        with urlopen(request, timeout=LLM_TIMEOUT) as response:
            result = json.load(response)
    except HTTPError as exc:
        # Do not echo provider bodies, which may include credentials or request text.
        exc.close()
        raise RuntimeError(f"LLM API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("LLM API request failed or timed out") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("LLM API returned invalid JSON") from None
    try:
        reply = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("LLM API returned no assistant message") from None
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("LLM API returned an empty assistant message")
    return reply.strip()
