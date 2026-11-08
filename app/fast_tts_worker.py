"""Resident offline CPU speech models, controlled by the assistant over JSON lines."""

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    assets = args.assets.resolve()
    os.chdir(assets)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    os.environ["MKL_NUM_THREADS"] = str(args.threads)
    wire = sys.stdout

    def send(payload):
        print(json.dumps(payload, ensure_ascii=False), file=wire, flush=True)

    # Libraries may print diagnostics; stdout is reserved for the wire protocol.
    with redirect_stdout(sys.stderr):
        import numpy as np
        import soundfile as sf
        import torch
        from pocket_tts import TTSModel

        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        russian = torch.package.PackageImporter(str(assets / "silero-v5_6_ru.pt")).load_pickle(
            "tts_models", "model")
        russian.to("cpu")
        english = TTSModel.load_model(config=str(assets / "pocket.yaml"))
        voice = english.get_state_for_audio_prompt(assets / "pocket-alba.safetensors")

        def generate(text, lang):
            if lang == "ru":
                with torch.inference_mode():
                    yield russian.apply_tts(text=text, speaker="xenia", sample_rate=24000,
                                            put_accent=True, put_yo=True).numpy()
            elif lang == "en":
                for chunk in english.generate_audio_stream(voice, text):
                    yield chunk.detach().cpu().numpy()
            else:
                raise ValueError("lang must be en or ru")

        # Warm both inference paths before reporting readiness.
        list(generate("Привет.", "ru"))
        list(generate("Hello.", "en"))
    send({"ready": True})
    if args.check:
        return

    for line in sys.stdin:
        output = None
        try:
            request = json.loads(line)
            text = str(request["text"]).strip()
            lang = request["lang"]
            if not text or len(text) > 2000:
                raise ValueError("text must contain between 1 and 2000 characters")
            output = Path(request["output"])
            started = time.monotonic()
            first = None
            chunks = []
            with redirect_stdout(sys.stderr):
                for chunk in generate(text, lang):
                    if first is None:
                        first = time.monotonic() - started
                    chunks.append(chunk.reshape(-1))
            generation = time.monotonic() - started
            audio = np.concatenate(chunks)
            if not audio.size or not np.isfinite(audio).all():
                raise RuntimeError("TTS returned empty or invalid audio")
            sf.write(output, audio, 24000, subtype="PCM_16")
            seconds = len(audio) / 24000
            send({"ok": True, "engine": "Silero v5.6 / Xenia" if lang == "ru" else "Pocket TTS 2M / Alba",
                  "language": lang, "threads": args.threads,
                  "generation_seconds": round(generation, 4), "first_chunk_seconds": round(first, 4),
                  "audio_seconds": round(seconds, 4), "rtf": round(generation / seconds, 4)})
        except Exception as exc:
            if output is not None:
                output.unlink(missing_ok=True)
            send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
