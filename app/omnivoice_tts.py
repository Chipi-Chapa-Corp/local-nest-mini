"""Offline, CPU-only OmniVoice worker with per-request resource reporting."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import resource
import sys
import time

MODEL = "k2-fsa/OmniVoice"
REVISION = "c5fdb5ccb189668d56333f77ba2629f4cd7535f4"
SOURCE_REVISION = "08be0b4ccbac3e13e374e86fbfead4b4cac343e2"
REFERENCE_TEXT = "We finally made breakfast without burning the toast."
VOICE_INSTRUCTION = "female, young adult"


def generation_options(lang, reference, steps=None, bootstrap_reference=False):
    options = dict(language=lang, num_step=steps or (32 if lang == "ru" else 16),
                   guidance_scale=2.0, normalize_text=False)
    if bootstrap_reference:
        if lang != "en":
            raise ValueError("Reference bootstrap requires English")
        options["instruct"] = VOICE_INSTRUCTION
    else:
        options.update(ref_audio=str(reference), ref_text=REFERENCE_TEXT)
    return options


def load_model(model_dir):
    import torch
    from omnivoice import OmniVoice
    from omnivoice.models.omnivoice import OmniVoiceConfig
    from app.omnivoice_cpu import configure_cpu_inference
    config = OmniVoiceConfig.from_pretrained(str(model_dir), local_files_only=True)
    config.dtype = config.llm_config.dtype = torch.bfloat16
    model = OmniVoice.from_pretrained(str(model_dir), config=config, device_map="cpu",
        dtype=torch.bfloat16, attn_implementation="sdpa", local_files_only=True).eval()
    configure_cpu_inference(model, precision="bf16", split_guidance=True)
    if any(parameter.dtype != torch.float32 for parameter in model.audio_tokenizer.parameters()):
        raise RuntimeError("OmniVoice audio codec must remain FP32")
    import gc
    gc.collect()
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lang", choices=("en", "ru"), required=True)
    parser.add_argument("--steps", type=int, choices=(16, 24, 32))
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--bootstrap-reference", action="store_true",
                        help="Setup only: generate an English reference without an existing voice")
    args = parser.parse_args()
    if not 1 <= args.threads <= 64 or not args.text.strip():
        parser.error("nonempty text and 1–64 threads required")
    if args.bootstrap_reference and args.lang != "en":
        parser.error("Reference bootstrap requires English")
    if not args.bootstrap_reference and not args.reference.is_file():
        parser.error("Voice reference is missing; run host/setup-omnivoice.sh")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        HF_DEACTIVATE_ASYNC_LOAD="1", OMP_NUM_THREADS=str(args.threads),
        MKL_NUM_THREADS=str(args.threads), OPENBLAS_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false",
        CUDA_VISIBLE_DEVICES="", HIP_VISIBLE_DEVICES="", ROCR_VISIBLE_DEVICES="")
    # Support the server invoking this file with the isolated model interpreter.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    started = time.monotonic()
    import numpy as np
    import soundfile as sf
    import torch
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    try:
        with redirect_stdout(sys.stderr):
            model = load_model(args.model_dir)
            load_seconds = time.monotonic() - started
            torch.manual_seed(1234)
            np.random.seed(1234)
            options = generation_options(args.lang, args.reference, args.steps, args.bootstrap_reference)
            before = resource.getrusage(resource.RUSAGE_SELF)
            generation_start = time.monotonic()
            with torch.inference_mode():
                audio = model.generate(text=args.text, **options)[0]
            generation_seconds = time.monotonic() - generation_start
            usage = resource.getrusage(resource.RUSAGE_SELF)
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            if not len(audio) or not np.isfinite(audio).all() or np.sqrt(np.mean(audio**2)) <= 1e-5:
                raise RuntimeError("OmniVoice returned empty, silent or invalid audio")
            sf.write(args.output, audio, model.sampling_rate, subtype="PCM_16")
        cpu_seconds = usage.ru_utime + usage.ru_stime - before.ru_utime - before.ru_stime
        duration = len(audio) / model.sampling_rate
        report = dict(backend="omnivoice", model=MODEL, revision=REVISION,
            source_revision=SOURCE_REVISION, engine="PyTorch CPU / SDPA",
            precision="BF16 transformer / FP32 codec", language=args.lang,
            voice=VOICE_INSTRUCTION if args.bootstrap_reference else "English reference clone",
            steps=options["num_step"], threads=args.threads, normalize_text=False,
            load_seconds=round(load_seconds, 3), generation_seconds=round(generation_seconds, 3),
            audio_seconds=round(duration, 3), rtf=round(generation_seconds/duration, 3),
            cpu_seconds=round(cpu_seconds, 3), average_cpu_cores=round(cpu_seconds/generation_seconds, 2),
            peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1),
            sample_rate=model.sampling_rate)
        print(json.dumps(report), flush=True)
    except BaseException:
        args.output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
