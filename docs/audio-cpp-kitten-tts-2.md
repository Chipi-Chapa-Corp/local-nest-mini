# audio.cpp Kitten TTS 2 CPU trial — 2026-10-23

Tested on the local AMD Ryzen 5 7600X (6 physical cores / 12 logical CPUs), approximately 30 GiB RAM. CPU only. Measurements were sequential, outside the running service cgroup, on the shared desktop. These are individual trial results rather than a statistically controlled benchmark.

## Model and build

- audio.cpp commit `c8365f391307a97bfe133e0872e632fe572ecace`; native CPU Release build with GCC 16.1.1, OpenMP and `-march=native`.
- Full **Kitten TTS 2, 1.7B**, recommended **2026-10-21 Q8** package with BF16 embedding/head. Full default S3 MeanFlow / HiFT decoder; no Mini or student decoder.
- Model repository `audio-cpp/audio.cpp-gguf`, revision `de8b09820cb1d274e18c93534280fc51049096ad`.
- `kitten-tts2-20261021-q8_0.gguf`: 3,343,404,128 bytes; verified SHA-256 `cd67d70466344b41e5e6c7fa317d09e88f499d2973e030d35c969dc8c7c84ff5`.
- All requests use `preset=expressive`, seed 1234 and max_tokens 1000. English voice: Bruno. Russian voice: Russian.
- audio.cpp passes text through without the upstream English normalizer. All expression markup reaches the model verbatim.
- Inference uses native GGML throughout, without Python, LibTorch or ONNX Runtime. Python is used here only to orchestrate and validate the trial.

## Thread scaling

Each process loads once and runs English twice followed by Russian twice. First-request timings include cold execution/graph setup after session preparation; warm timings are the immediate repeat in the same session. All times below exclude model/session loading. Identical seeded repeats produced byte-identical WAVs within each thread setting. Different thread counts can generate different speech lengths.

| Threads | English first / warm | Russian first / warm | Peak process RAM | Mean CPU equivalents |
|---|---:|---:|---:|---:|
| 6 | 8.36 / 6.55 s | 8.57 / 7.00 s | 4.74 GiB | 5.10 |
| 8 | 9.28 / 7.07 s | 10.93 / 8.44 s | 4.77 GiB | 6.80 |
| 12 | 19.49 / 17.09 s | 21.20 / 18.04 s | 4.74 GiB | 10.06 |

CPU equivalents are process user+system CPU seconds divided by process wall time, including loading, preparation, synthesis and output. RAM is Linux peak RSS for the whole process, not virtual address space. Six threads was fastest among the tested settings.

## Comparison with the existing Kitten C++ runtime

Both implementations received identical text and the expressive preset. The existing runtime uses six LM threads and two LibTorch decoder threads, normalizing English but not Russian. Its model remains pinned to revision `1f00fee525c7b2e30bbe9ca9ba67529734fce9c9`, packed TQ2_1. A subsequent correctness audit verified that all 314 language-model tensors in the audio.cpp Q8 package match this checkpoint exactly; the packings are lossless. This compares the actual installed configurations: arithmetic, sampling RNGs and generated lengths still differ. These timings predate the silence-penalty correction documented in the [correctness audit](tts-controls-correctness.md).

| Runtime | Language | First / warm generation | Audio duration | Warm RTF | Peak RAM |
|---|---|---:|---:|---:|---:|
| Existing kitten-tts-cpp 0.2.0 | en | 14.93 / 15.50 s | 3.46 s | 4.48 | 2.27 GiB |
| audio.cpp, six threads | en | 8.36 / 6.55 s | 3.62 s | 1.81 | 4.74 GiB |
| Existing kitten-tts-cpp 0.2.0 | ru | 16.47 / 16.59 s | 3.96 s | 4.19 | 2.23 GiB |
| audio.cpp, six threads | ru | 8.57 / 7.00 s | 3.96 s | 1.77 | 4.74 GiB |

Cold audio.cpp CLI process wall times, including model/session creation, preparation, one synthesis and writing the WAV: en 15.36 s, ru 16.04 s. For the existing worker, model loading plus first synthesis was en 17.77 s, ru 18.79 s. Model/session startup reduces the advantage when launching a fresh process for every reply.


RTF is generation seconds divided by audio seconds; lower is better and less than one means faster than real time. The current systemd service has a 3 GiB hard memory cap. The measured audio.cpp Q8 process exceeds that cap, so this trial does not establish that it can run in the service unchanged.

## Emotion and sound files

46 WAVs are saved, covering English and Russian: neutral references, all ten listed emotions, ten supported sound tags, the original `gaso` spelling as an extra control, and emphasis. Each emotion uses the same sentence within its language so changes in delivery are easier to compare. All files were checked for valid mono 24 kHz PCM16 data and nonzero audio energy. Emotions and vocal effects require listening review; successful generation does not establish their perceptual quality.

**The original prompt contains `<gaso>`. The runtime recognizes `<gasp>`.** Both are included to make this difference audible; the assistant prompt was not edited in this experiment.

- [Listening gallery](../data/benchmarks/audio-cpp-kitten2/index.html)
- [Sample list with exact text](../data/benchmarks/audio-cpp-kitten2/SAMPLES.md)
- [Download all samples](../data/benchmarks/audio-cpp-kitten2/voice-samples.zip)
- [Machine-readable results](../data/benchmarks/audio-cpp-kitten2/results.json)
- [Local Whisper transcriptions](../data/benchmarks/audio-cpp-kitten2/asr-results.json)

Local Whisper/FUTO transcription was run on all 46 samples. 20/20 emotion samples matched the normalized reference words exactly. Transcription can omit/describe vocal sounds and mishear names, so it is an intelligibility spot check, not an emotion or audio quality score. Exact transcriptions are included in the gallery.

## Reproduce

The source, build, downloaded model and raw logs are retained in ignored local directories. The trial did not change the TTS integration or restart the service.

```sh
models/audio-cpp-tools/bin/cmake -S models/audio-cpp-src -B models/audio-cpp-src/build-kitten2 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DAUDIOCPP_MODEL_SET=custom -DAUDIOCPP_MODELS=kitten_tts2 \
  -DENGINE_ENABLE_CUDA=OFF -DENGINE_BUILD_MODEL_TESTS=ON -DAUDIOCPP_DEPLOYMENT_BUILD=ON \
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++
models/audio-cpp-tools/bin/cmake --build models/audio-cpp-src/build-kitten2 --target audiocpp_cli --parallel 6
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py pilot --threads 6
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py pilot --threads 8
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py pilot --threads 12
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py samples
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py cold
.venv/bin/python data/benchmarks/audio-cpp-kitten2/run_benchmark.py baseline
.venv/bin/python data/benchmarks/audio-cpp-kitten2/inspect_samples.py
.venv/bin/python data/benchmarks/audio-cpp-kitten2/summarize.py
```

Per-run commands, request sequences, generation metrics, GNU time resource reports, manifests and logs are in `data/benchmarks/audio-cpp-kitten2/`. Model weights remain under `models/audio-cpp-kitten2/`.

Powered by Stellon Labs. Upstream: [audio.cpp Kitten docs](https://github.com/0xShug0/audio.cpp/blob/c8365f391307a97bfe133e0872e632fe572ecace/docs/models/kitten_tts2.md), [model package](https://huggingface.co/audio-cpp/audio.cpp-gguf/tree/de8b09820cb1d274e18c93534280fc51049096ad/KittenTTS2-GGUF).

## Control-following follow-up

Listening feedback found weak emotions in both languages and missing vocal events, especially in English. ASR word accuracy does not validate those effects. The [controlled correctness audit](tts-controls-correctness.md) records exact input/weight comparisons, a reproduced sampling mismatch and decoder component tests, and provides paired reference/native audio. The voice-variation sweep was stopped.
