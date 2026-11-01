"""Keenable search and page reading, matching context-tg's web tools."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

API_URL = "https://api.keenable.ai/v1"
MAX_OUTPUT_BYTES = 16000


def _definition(name, description, argument, argument_description):
    return {"type": "function", "function": {
        "name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": {
            argument: {"type": "string", "description": argument_description},
        }, "required": [argument], "additionalProperties": False},
    }}


TOOL_DEFINITIONS = [
    _definition("web_search",
                "Search the web for current facts and source links. Read relevant pages "
                "with read_web_page to verify them; snippets alone are not evidence.",
                "query", "One focused search query."),
    _definition("read_web_page", "Read a web page through Keenable to verify a search result.",
                "url", "The HTTP(S) URL of the relevant page."),
]


def available() -> bool:
    return bool(os.environ.get("KEENABLE_API_KEY", "").strip())


def _request(path, *, data=None, timeout):
    key = os.environ.get("KEENABLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Web search is not configured")
    request = Request(API_URL + path, data=data, headers={
        "X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        exc.close()
        raise RuntimeError(f"Keenable returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Keenable request failed or timed out") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("Keenable returned invalid JSON") from None


def _execute(name: str, arguments: str) -> str:
    args = json.loads(arguments)
    argument = {"web_search": "query", "read_web_page": "url"}.get(name)
    if argument is None:
        raise ValueError("Unknown tool")
    if not isinstance(args, dict) or set(args) != {argument}:
        raise ValueError(f"Tool requires only {argument}")
    value = args[argument]
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError(f"{argument} must be a nonempty string of at most 2000 characters")
    value = value.strip()
    if name == "web_search":
        payload = _request("/search", data=json.dumps({"query": value}).encode(), timeout=20)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise RuntimeError("Keenable returned no search results array")
        return json.dumps([{k: v for k, v in item.items() if k != "acquired_at"}
                           for item in results if isinstance(item, dict)], ensure_ascii=False)
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError("url must be an HTTP(S) URL")
    payload = _request("/fetch?" + urlencode({"url": value}), timeout=30)
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Keenable returned no page content")
    return content


def execute(name: str, arguments: str) -> str:
    try:
        output = _execute(name, arguments)
    except (ValueError, TypeError, RuntimeError) as exc:
        output = json.dumps({"error": str(exc)}, ensure_ascii=False)
    encoded = output.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n[Tool result truncated.]"
    return output
