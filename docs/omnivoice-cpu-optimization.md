# OmniVoice CPU optimization — 2026-10-23

The optimized path uses CPU only, respecting the user's requirement to avoid GPU inference unless it stays below 1 GiB VRAM. No GPU backend was used. The approved English recording is preserved; the [listening page](../data/benchmarks/tts-correctness/index.html) has exactly two players with recording selectors for references and candidates. Failed candidates are excluded.

## Full-passage measurements

| Recording | Steps/chunk | Generation | Speedup vs reference | Script words/s | Peak process RSS | Speech-check coverage |
|---|---:|---:|---:|---:|---:|---:|
| English reference | 32 | 411.55 s | 1.00× | 0.282 | 3.02 GiB | 116/116 |
| English CPU candidate | 32 | 117.00 s | 3.52× | 0.991 | 4.59 GiB | 114/116 |
| English CPU candidate, selected | 16 | 83.05 s | 4.96× | 1.397 | 4.49 GiB | 115/116 |
| Russian reference, cloned English voice | 32 | 280.34 s | 1.00× | 0.321 | 3.50 GiB | 88/90 |
| Russian CPU candidate, selected | 32 | 107.18 s | 2.62× | 0.840 | 5.15 GiB | 90/90 |

English output is 47.42 seconds for all three variants. Selected Russian output is 58.80 seconds versus 58.29 seconds for its reference. Selected average CPU usage is 572% for English and 595% for Russian (100% = one logical CPU fully busy). Selected RTFs are 1.75 and 1.82: these full passages still take longer to generate than their playback duration. System RAM consumption increased; this is a speed/memory tradeoff, not a claim of reduced RAM use.

These are individual runs on a shared Ryzen 5 7600X host (six physical cores/twelve logical CPUs), with six inference threads. No power/energy measurement was made. Generation includes waveform decoding and postprocessing; Python imports/model loading are recorded separately. Speech checks run afterward and do not enter synthesis timings. CPU contention, clock changes, and cache state can affect wall times; the 16-step result is not assumed to halve total runtime exactly.

**Listening acceptance remains pending.** The faster English transcript omitted `it` in `It turns out...`; the 32-step English transcript differed at `us too`. The original English remains the listener-approved reference. Word recognition does not establish naturalness, pronunciation accuracy, or successful sound-tag rendering. All candidates use the identical text and the same 13 requested native sound tags.

The Russian 16-step trial took 67.64 seconds but recovered only 57/90 target words and dropped several phrases. It was rejected and is not on the active page. The selected Russian run retains 32 steps and recovered all target words.

## Implementation

[app/omnivoice_cpu.py](../app/omnivoice_cpu.py) provides an opt-in configuration for the official OmniVoice model:

- Keep the audio codec in FP32. Cast only the transformer and its audio embedding/output projection to BF16.
- Disable the unused transformer KV cache. MaskGIT recomputes its context each refinement step.
- Evaluate conditional and unconditional guidance sequences separately at their actual lengths. Upstream pads both to the longer conditional sequence; the unconditional padding has no attention connection to the target tokens.
- Project only target hidden states through the audio output head. Unused context logits are filled to preserve the upstream sampler's indexing contract.
- Preserve the official sampler, guidance scale, temperatures, text normalization settings, and seed. Other batch/layout cases fall back to the original forward method.

The helper rejects non-CPU models. The benchmark explicitly loads all model/codec components on CPU and checks codec parameter precision. Changes are applied to the in-memory model; the pinned upstream source checkout is unchanged. The production service backend has not been switched to OmniVoice during this audition.

## Processor integration

After the audition, the user selected this model for the processor. `app/omnivoice_tts.py` now wraps this CPU path as an offline worker; `app/server.py` selects it by default and serializes requests. English uses 16 steps and Russian uses 32, both with the preserved reference at `models/omnivoice-voices/laylo-en.wav`. The worker reports actual per-request generation/CPU/RAM metrics and removes partial output on failure. Cancellation terminates the child process and releases the model slot.

The worker reproduced the earlier two-sentence English BF16 probe byte-for-byte, validating parity with the benchmark. A new short English input (`The new voice is ready. [laughter] We can start now.`) nevertheless collapsed into noise under voice-design conditioning. The same text, seed, steps and CPU settings with reference conditioning produced the complete spoken text. The runtime therefore uses reference conditioning in English as well as Russian. This changes conditioning relative to the full-passage English speed benchmark above; the earlier 4.96×/0.72 seconds-per-word result is not a measurement of the final reference-conditioned English worker. The new short reference-conditioned test took 5.36 seconds for 4.12 seconds of audio. Speech recognition confirms text, not pronunciation, laughter or other effects. Integration outputs/checks are retained in `data/benchmarks/omnivoice/integration/`, off the active listening page.

Laylo's LLM system prompt uses the 13 native OmniVoice sound tags; Kitten-specific emotion labels, angle-bracket sounds and triple-parenthesis controls are removed, including the old backend-dependent prompt. Normalization is disabled in both OmniVoice languages, and the prompt asks the LLM to write numbers as spoken words.

`bash host/setup-omnivoice.sh` prepares pinned source/model assets and the CPU environment. This installation preserves the exact approved synthetic reference waveform. The host unit is raised to 6 GiB MemoryHigh / 8 GiB MemoryMax; existing installations must run `./run.sh install` to apply the unit and restart, rather than only restarting under the old 3 GiB cap. The current environment requires a host sudo password for that privileged installation. Compose mounts the model/reference read-only and installs a separate CPU environment in its image; the updated image has not been built in this host session.

Integration verification: 74 project tests pass (the four optional CPU tests are skipped there and pass separately in the OmniVoice environment). Server/worker smoke tests recover the complete short English greeting, the short English sentence that failed in design mode, and the Russian readiness sentence. The runtime's health/model endpoints report OmniVoice and both languages correctly on an isolated HTTP fixture. The installed service still reports Kitten until the privileged unit installation/restart; no activation is claimed. Integration ASR checks validate words only, not naturalness or sound events.

### Host-service dependency repair

After service activation, a live `/answer` check reproduced a TTS crash: `ModuleNotFoundError: No module named 'numpy'`. The audition environment had `include-system-site-packages=true` and obtained NumPy and several other dependencies from the developer's user site; the root-run host service could not see them. The model environment is now isolated (`include-system-site-packages=false`) and its missing dependency closure is installed locally. `pip check` and host-side imports pass without user-site packages, and the same live `/answer` request now succeeds with generated OmniVoice audio.

The setup script normalizes pre-existing environments to isolated mode, disables user-site discovery while installing dependencies, and verifies imports with Python `-I`. Host startup checks the model imports in isolated mode before opening the assistant service, so this missing-dependency condition cannot be hidden behind asset-only readiness checks. The environment repair takes effect on the next TTS child without restarting the service. This fixes the confirmed TTS error; Nest capture/transport is verified separately, since no new S3 voice request had reached the service during the reported instant-error attempt.

The driver in [host/benchmarks/run_omnivoice.py](../host/benchmarks/run_omnivoice.py) exposes CPU mode, step count, thread count and a separate output folder. CPU experiments require an explicit output path to preserve accepted recordings. It avoids concurrent weight-loading workers and returns temporary CPU allocation buffers after loading. The selected decoder remains unquantized F32; no `torch.compile` or GPU kernels were used.

## Numerical and speed checks

On actual first-chunk inputs, with three timed repetitions per configuration:

| Forward path | English median | Russian median |
|---|---:|---:|
| Upstream F32 | 1.524 s | 2.008 s |
| Separate guidance, F32 | 1.389 s | 1.636 s |
| Separate guidance, BF16 | 0.677 s | 0.832 s |

The F32 restructuring matched the top token at every checked target position; maximum used-logit error was below `7.7e-5`. BF16 changed logits and token choices, so it is not described as numerically lossless or assumed to retain the approved voice's quality. End-to-end candidates were generated and checked independently.

[tests/test_omnivoice_cpu.py](../tests/test_omnivoice_cpu.py) checks target-logit preservation with varying padding/audio codes, codec precision, and fallback behavior using a small bidirectional attention model. These tests run in the separate OmniVoice environment; normal project tests can skip them when OmniVoice dependencies are absent. The real-model timing/parity artifacts are retained in `data/benchmarks/omnivoice/optimization/`.

The initial 4/6/8-thread sweep favored six threads for BF16 on both tested inputs. A loading adjustment produced a byte-identical short WAV to the prior BF16 loader path, so it was not counted as a quality improvement.

## Native CPU alternative tested

Built `ServeurpersoCom/omnivoice.cpp@1a468d8b9d65b3544ca1004d2d7c05dfc65d1f3e` with `ggml@cc5df49a6b878db2b68b9a7b45976e279594627d`, Release, native CPU instructions, OpenMP, six CPU threads, no BLAS/GPU backend. Converted the existing pinned official weights locally to F32 GGUF and derived Q8_0 transformer weights; codec weights remained F32.

The Q8_0 CPU trial on the two-sentence English probe, at 32 steps, took 36.28 seconds process wall time (35.39 seconds synthesis/decode) for 7.24 seconds audio, with 1.84 GiB peak RSS. The optimized PyTorch 32-step probe took 10.10 seconds synthesis/decode for 7.20 seconds audio, with 4.38 GiB peak RSS. The native build uses less RAM but was not selected for the speed-focused path. This observation applies to this build/configuration, not every native backend. No native-engine quality acceptance is claimed. Raw timings are in `optimization/native-probe/` and `optimization/torch-probe32/`.

## Final verification

- All four CPU-specific unit tests pass in the OmniVoice environment.
- The project suite passes: 67 discovered tests, with the four OmniVoice-specific tests skipped in the ordinary environment and run separately above.
- All five generated review URLs contain exactly two players. Every selected recording, resource graph, CSV, and results JSON exists, and displayed timings/RAM match the results files.
- The review JavaScript passes syntax checking. A DOM fixture exercises every recording selector and confirms audio, graphs, transcripts, measurements, and download links update together; playing one track pauses the other.
- The rejected Russian 16-step run is excluded from the active selectors. No service restart or production-backend change was made.

## Reproduce selected candidates

Pinned model/source and the English reference crop are described in [the original audition report](omnivoice-sound-suite.md). Torch is `2.11.0+cpu`, Transformers `5.20.2`; the same seed 1234, guidance 2.0 and normalization-off settings are retained.

```sh
models/omnivoice-env/bin/python host/benchmarks/run_omnivoice.py --only en --cpu-mode split-bf16 --steps 16 --output data/benchmarks/omnivoice/optimization/fast16-en
models/omnivoice-env/bin/python host/benchmarks/run_omnivoice.py --voice-reference --only ru --cpu-mode split-bf16 --steps 32 --output data/benchmarks/omnivoice/optimization/quality32-ru
.venv/bin/python host/benchmarks/check_omnivoice_speech.py data/benchmarks/omnivoice/optimization/fast16-en --case en
.venv/bin/python host/benchmarks/check_omnivoice_speech.py data/benchmarks/omnivoice/optimization/quality32-ru
.venv/bin/python host/benchmarks/review_omnivoice_optimized.py
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 models/omnivoice-env/bin/python -m unittest tests.test_omnivoice_cpu -v
```

The review builder exposes only candidates that pass the transcript-coverage gate. Its selectors update both the recording and corresponding CPU/RAM graphs, measurements, transcript, and downloads. Detailed WAVs, JSON, resource CSVs, standalone SVG/PNG graphs, logs and `selected-results.json` remain under `data/benchmarks/omnivoice/optimization/`.

Primary references: [upstream generation settings](https://github.com/k2-fsa/OmniVoice/blob/08be0b4ccbac3e13e374e86fbfead4b4cac343e2/docs/generation-parameters.md), [native CPU port](https://github.com/ServeurpersoCom/omnivoice.cpp/tree/1a468d8b9d65b3544ca1004d2d7c05dfc65d1f3e), [PyTorch CPU BF16](https://pytorch.org/blog/empowering-pytorch-on-intel-xeon-scalable-processors-with-bfloat16/). CPU speedups in this report are measurements on this AMD host, not the Intel benchmark figures in the PyTorch article.
