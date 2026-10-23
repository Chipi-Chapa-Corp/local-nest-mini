"""Run a KittenTTS model in its CPU-only environment and report actual usage."""

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import resource
import sys
import time


MODEL = "KittenML/kitten-tts-2"
REVISION = "1f00fee525c7b2e30bbe9ca9ba67529734fce9c9"
MINI_MODEL = "KittenML/kitten-tts-mini-0.8"
MINI_REVISION = "c02725660cea441db4c383af69f1f26f5cd00947"


def _load_mini(args):
    os.environ["HF_HUB_OFFLINE"] = "1"
    import onnxruntime as ort
    from kittenml import KittenTTS

    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    original = ort.InferenceSession
    # Upstream does not expose session options. This separate worker constructs
    # exactly one model; restore the constructor immediately after loading it.
    def session(*positional, **kwargs):
        kwargs["sess_options"] = options
        return original(*positional, **kwargs)
    ort.InferenceSession = session
    try:
        return KittenTTS(str(args.cache_dir), backend="cpu")
    finally:
        ort.InferenceSession = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--family", choices=("2", "mini"), default="2")
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", default="Bruno")
    parser.add_argument("--decoder", default="default")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--decoder-threads", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--download", action="store_true", help="Allow model asset downloads")
    args = parser.parse_args()
    if not 1 <= args.threads <= 64 or not 1 <= args.decoder_threads <= 64 or args.repeat < 1:
        parser.error("invalid thread count or repeat count")
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    os.environ["MKL_NUM_THREADS"] = str(args.decoder_threads)
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    started = time.monotonic()
    import soundfile as sf

    if args.family == "mini":
        model = _load_mini(args)
    else:
        from kitten_tts_cpp import KittenTTS
        model = KittenTTS(MODEL, revision=REVISION, cache_dir=str(args.cache_dir),
                          decoder=args.decoder, threads=args.threads,
                          decoder_threads=args.decoder_threads, offline=not args.download)
    try:
        load_seconds = time.monotonic() - started
        for iteration in range(args.repeat):
            cpu_start = resource.getrusage(resource.RUSAGE_SELF)
            generation_start = time.monotonic()
            with redirect_stdout(sys.stderr):
                if args.family == "mini":
                    model.generate_to_file(args.text, args.output, voice=args.voice)
                else:
                    model.generate_to_file(args.text, args.output, voice=args.voice,
                                           advanced={"seed": 1234})
            generation_seconds = time.monotonic() - generation_start
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu_seconds = (usage.ru_utime + usage.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime)
            info = sf.info(args.output)
            report = {
                "backend": "kitten-mini" if args.family == "mini" else "kitten",
                "model": MINI_MODEL if args.family == "mini" else MODEL,
                "revision": MINI_REVISION if args.family == "mini" else REVISION,
                "decoder": "onnx" if args.family == "mini" else args.decoder, "voice": args.voice,
                "threads": args.threads,
                "decoder_threads": None if args.family == "mini" else args.decoder_threads,
                "iteration": iteration + 1, "load_seconds": round(load_seconds, 3),
                "generation_seconds": round(generation_seconds, 3),
                "audio_seconds": round(info.duration, 3),
                "rtf": round(generation_seconds / info.duration, 3) if info.duration else None,
                "cpu_seconds": round(cpu_seconds, 3),
                "average_cpu_cores": round(cpu_seconds / generation_seconds, 2),
                "peak_rss_mib": round(usage.ru_maxrss / 1024, 1),
                "sample_rate": info.samplerate,
            }
            print(json.dumps(report), flush=True)
    finally:
        if hasattr(model, "close"):
            model.close()


if __name__ == "__main__":
    main()
