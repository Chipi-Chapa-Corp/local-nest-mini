"""Normalize OpenAI Responses output and SSE into the assistant's tool loop."""

import json


def message(result):
    if not isinstance(result, dict) or not isinstance(result.get("output"), list):
        raise RuntimeError("LLM API returned no response output")
    if result.get("status") not in {None, "completed"}:
        raise RuntimeError("LLM API ended the response before it was complete")
    texts, calls = [], []
    for item in result["output"]:
        if item.get("type") == "function_call":
            calls.append({"id": item.get("call_id"), "type": "function", "function": {
                "name": item.get("name"), "arguments": item.get("arguments"),
            }})
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    normalized = {"role": "assistant", "content": "".join(texts),
                  "response_output": result["output"]}
    if calls:
        normalized["tool_calls"] = calls
    return normalized


def stream_message(response, on_delta=None, check=None):
    data = []
    calls_seen = False
    text_seen = False
    for raw in response:
        if check:
            check()
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line or not data:
            continue
        event = json.loads("\n".join(data))
        data = []
        kind = event.get("type")
        if kind == "response.output_item.added" and event.get("item", {}).get("type") == "function_call":
            calls_seen = True
        elif kind == "response.output_text.delta":
            text = event.get("delta")
            if on_delta and not calls_seen and isinstance(text, str):
                on_delta(text)
                text_seen = True
        elif kind == "response.completed":
            result = message(event.get("response"))
            if on_delta and not text_seen and not result.get("tool_calls"):
                on_delta(result["content"])
            return result
        elif kind in {"response.failed", "response.incomplete", "error"}:
            raise RuntimeError("LLM API reported an incomplete or failed response")
    raise RuntimeError("LLM API stream ended before completion")
