"""Minimal local processor used until a real assistant is wired in."""


def respond(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("input must not be empty")
    return f"I have heard: {text}"
