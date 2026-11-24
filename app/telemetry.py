"""Usage and memory measurements for pipeline stages."""

import math
from pathlib import Path


# USD per million tokens. Voice Live bills speech and text through its tier.
# https://learn.microsoft.com/azure/ai-services/speech-service/voice-live#pricing
# https://prices.azure.com/api/retail/prices (Azure Speech, East US, USD)
VOICE_LIVE_RATES = {
    "gpt-6.1-sol": ((4, .4, 16), (17, .4, 31)),
    "gpt-6-luna": ((.11, .04, .44), (15, .04, 25)),
}
# Global Standard, short-context Azure OpenAI rates, outside Voice Live.
# https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-gpt-6-1-sol-in-microsoft-foundry-advanced-intelligence-optimized-for/4560811
# https://azure.microsoft.com/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/
TEXT_RATES = {"gpt-6.1-sol": (2, .1, 10), "gpt-6-luna": (.1, .01, .5)}


def number(value):
    return (value if type(value) in (int, float) and math.isfinite(value) and value >= 0
            else None)


def _usage(input_tokens, cached_tokens, output_tokens, rates, reported=None):
    counts = {"input_tokens": input_tokens, "cached_tokens": cached_tokens,
              "output_tokens": output_tokens}
    result = {"usage": {key: value for key, value in counts.items() if value is not None}}
    if reported is not None:
        result.update(cost_usd=reported, cost_estimated=False)
    elif rates and input_tokens is not None and output_tokens is not None:
        cached = min(input_tokens, cached_tokens or 0)
        cost = ((input_tokens - cached) * rates[0] + cached * rates[1]
                + output_tokens * rates[2]) / 1_000_000
        result.update(cost_usd=cost, cost_estimated=True)
    return result


def voice_usage(payload, model):
    usage = payload.get("usage") or {}
    inputs = usage.get("input_token_details") or {}
    outputs = usage.get("output_token_details") or {}
    cached = inputs.get("cached_tokens_details") or {}
    text_rate, audio_rate = VOICE_LIVE_RATES.get(model, (None, None))
    text_output = number(outputs.get("text_tokens"))
    audio_output = number(outputs.get("audio_tokens"))
    total_output = number(usage.get("output_tokens"))
    if audio_output is not None and total_output is not None and total_output >= audio_output:
        text_output = total_output - audio_output
    return {
        "transcribe": _usage(number(inputs.get("audio_tokens")), number(cached.get("audio_tokens")),
                             0, audio_rate, number(usage.get("transcription_cost_usd"))),
        "process": _usage(number(inputs.get("text_tokens")), number(cached.get("text_tokens")),
                          text_output, text_rate,
                          number(usage.get("llm_cost_usd"))),
        "tts": _usage(0, 0, audio_output, audio_rate,
                      number(usage.get("tts_cost_usd"))),
    }


def text_usage(payload, model, host):
    usage = payload.get("usage") or {}
    inputs = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    reported = number(payload.get("cost_usd", usage.get("cost_usd")))
    if reported is None and host == "openrouter.ai":
        reported = number(usage.get("cost"))
    rates = TEXT_RATES.get(model) if host.endswith(".openai.azure.com") else None
    return _usage(number(usage.get("input_tokens", usage.get("prompt_tokens"))),
                  number(inputs.get("cached_tokens")),
                  number(usage.get("output_tokens", usage.get("completion_tokens"))),
                  rates, reported)


def combine_usage(entries):
    if not entries:
        return {}
    tokens = {key: sum(row["usage"].get(key, 0) for row in entries)
              for key in entries[0]["usage"] if all(key in row["usage"] for row in entries)}
    result = {"usage": tokens}
    if all("cost_usd" in row for row in entries):
        result.update(cost_usd=sum(row["cost_usd"] for row in entries),
                      cost_estimated=any(row["cost_estimated"] for row in entries))
    return result


class ProcessPeak:
    """Sample Linux process high-water RSS and per-client DRM VRAM."""

    def __init__(self, pid, *, gpu=False):
        self.path = Path("/proc") / str(pid)
        self.gpu = gpu
        self.metrics = {} if gpu else {"peak_vram_mib": 0}

    def sample(self):
        try:
            for line in (self.path / "status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    self._peak("peak_rss_mib", int(line.split()[1]) / 1024)
                    break
        except (OSError, ValueError):
            pass
        if self.gpu:
            self._sample_vram()

    def _peak(self, name, value):
        self.metrics[name] = max(self.metrics.get(name, 0), value)

    def _sample_vram(self):
        clients = {}
        try:
            entries = list((self.path / "fdinfo").iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                fields = dict(line.split(":", 1) for line in entry.read_text().splitlines()
                              if ":" in line)
                vram = (fields.get("drm-resident-vram") or fields.get("drm-memory-vram")
                        or fields.get("drm-total-vram"))
                if vram:
                    client = (fields.get("drm-pdev"), fields.get("drm-client-id", entry.name))
                    size = int(vram.split()[0])
                    clients[client] = max(clients.get(client, 0), size)
            except (OSError, ValueError):
                continue
        if clients:
            self._peak("peak_vram_mib", sum(clients.values()) / 1024)
