# Kitten TTS Mini 80M trial — 2026-10-23

The next smaller official model after the 1.7B Kitten TTS 2 is
`KittenML/kitten-tts-mini-0.8` (80M). Below that are Micro 40M and Nano 15M,
including an INT8 Nano variant. These are the older ONNX family, not smaller
Kitten TTS 2 checkpoints. They offer eight fixed voices without voice cloning.

On the same Ryzen 5 7600X CPU used for the 1.7B test, with the same English
text and Bruno voice, Mini measured:

| Measurement | First generation | Second generation | Third generation |
| --- | ---: | ---: | ---: |
| Imports + model loading | 0.536 s | already loaded | already loaded |
| Synthesis | 0.310 s | 0.303 s | 0.301 s |
| Audio duration | 3.242 s | 3.242 s | 3.242 s |
| Peak process RSS | 276.3 MiB | 309.8 MiB | 309.8 MiB |
| Mean CPU during synthesis | 2.95 logical CPU equivalents | 3.04 | 3.05 |

For the first short reply, that is approximately **0.85 seconds including
loading**, versus **17.7 seconds** with the 1.7B model. Synthesis alone is
approximately 50 times faster for these particular samples, and runs about
10 times faster than real time. These are measurements of one short input,
not a quality comparison or a guarantee for all text lengths.

Input: `I have heard: Hello from our local Nest Mini.` The workers use six
intra-op threads, one inter-op thread, CPUExecutionProvider, and disabled
ONNX thread-pool spinning. No GPU or PyTorch is involved. Both workers are
short-lived, so model memory is released after a request. The first comparison
uses the actual configured worker path; later calls measure a loaded model
for reference. Peak RSS includes the full worker, and mean CPU is CPU seconds
divided by synthesis wall time. `/usr/bin/time -v` confirmed the peak RSS and
reported no swaps.

The ONNX file is 78,268,016 bytes, plus 3,278,902 bytes of voice data.
The model revision is pinned to `c02725660cea441db4c383af69f1f26f5cd00947`.
The isolated ONNX-only environment uses `kittenml==0.9.6`,
`onnxruntime==1.31.0`, and the dependencies listed by the upstream
[ONNX-only installation instructions](https://github.com/KittenML/KittenTTS/blob/main/docs/onnx-models.md).
The full package declares extra dependencies for its larger model; those
are deliberately omitted here, as described in those instructions.

The upstream Mini implementation uses an `en-us` phonemizer, so the integration
routes English to Mini and keeps Russian on the existing Pocket TTS voice.
The processor remains unchanged. Inference uses only the local prepared model
directory with Hugging Face offline mode enabled.

A separate host-side HTTP Test request also passed with real Whisper and Mini:
2.8 seconds end to end, including STT, processing, synthesis, and WAV retrieval.
Its TTS step took 0.564 seconds to load plus 0.361 seconds to synthesize
3.792 seconds of audio, with 280.2 MiB peak RSS. A real Russian synthesis
request confirmed the Pocket TTS route. All 48 regression tests passed.

Setup and activation:

```sh
./host/setup-kitten.sh mini
# Set TTS_BACKEND=kitten-mini in config.env, then:
./run.sh restart
```

Reproduce the short benchmark:

```sh
models/kitten-mini-env/bin/python app/kitten_tts.py --family mini \
  --cache-dir models/kitten-tts-mini-0.8 --threads 6 --repeat 3 \
  --text 'I have heard: Hello from our local Nest Mini.' \
  --output data/benchmarks/kitten-tts-mini/repeat.wav
```

Raw reports, sample WAVs, and time logs are under
`data/benchmarks/kitten-tts-mini/` (ignored by git).

References: [official lineup](https://github.com/KittenML/KittenTTS#available-models),
[Mini model](https://huggingface.co/KittenML/kitten-tts-mini-0.8),
[upstream English phonemizer](https://github.com/KittenML/KittenTTS/blob/main/kittenml/kittentts_legacy/onnx_model.py).
