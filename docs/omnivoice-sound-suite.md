# OmniVoice: Russian, English, and mixed speech

This audition follows the listener's finding that OmniVoice sounds much more natural than the Kitten samples. It tests all 13 upstream non-verbal controls in a meaningful breakfast-and-travel story, translated between Russian and English, plus two versions that switch languages within sentences. All four tracks use OmniVoice only.

The initial four-track audition is retained in the [benchmark archive](../data/benchmarks/omnivoice/sound-suite/index.html). Following listener feedback, the active [listening page](../data/benchmarks/tts-correctness/index.html) now focuses on the approved English recording and a fully Russian translation using that English voice as a reference. Failed noise and mixed-language tracks are removed from the active page.

The subsequent [CPU optimization report](omnivoice-cpu-optimization.md) records the selected faster candidates, rejected Russian 16-step run, and CPU/RAM tradeoffs. The current page keeps two players and uses selectors to preserve access to the original references.

## Listener results and measurements

| Initial track | Audio | Generation | RTF | Average CPU | Peak RSS |
|---|---:|---:|---:|---:|---:|
| Russian, voice design | 44.66 s | 434.33 s | 9.73 | 564% | 3.28 GiB |
| English, voice design | 47.42 s | 411.55 s | 8.68 | 560% | 3.02 GiB |
| Russian → English within sentences | 47.10 s | 369.05 s | 7.84 | 578% | 3.06 GiB |
| English → Russian within sentences | 44.90 s | 362.65 s | 8.08 | 580% | 3.18 GiB |

The listener accepted **English — same story** as excellent. Its 116 scripted words correspond to **0.282 script words per generation second** and **2.446 scripted words per audio second** (146.8 words/minute, including pauses and vocal sounds). A separate local Whisper pass recovered all story sentences, with vocal interjections represented as additional transcript tokens.

The Russian voice-design output failed: the listener heard noise, and a subsequent speech check recognized only non-speech annotations (`*звук птиц*`) rather than the requested story. A valid/non-silent waveform was not an adequate acceptance check; that failure should have been caught before publishing the audition. The listener also rated **Russian → English within sentences** poorly and asked to focus on a fully Russian translation. No quality acceptance is claimed for the remaining mixed track.

### Russian reference-voice follow-up

Upstream states that voice cloning is its most stable mode; voice design is trained on Chinese and English and can generalize unstably to other languages. The failed run used voice design. This is a reason to test reference-conditioned generation, not proof of the exact cause of the collapse.

A 4.32-second reference is cropped from the start of the accepted English recording, with the exact reference transcript `We finally made breakfast without burning the toast.`. No external voice sample or additional ASR model is loaded into OmniVoice. The target language remains explicitly `ru`; the full target text is the same all-Russian translation with all 13 tags. F32, CPU threads, seed, diffusion steps, normalization, and other generation defaults remain unchanged.

A two-sentence probe generated 9.72 seconds of audio in 56.21 seconds, averaging 577% CPU, with 3.29 GiB kernel peak RSS. A separate local speech check recovered the two Russian target sentences verbatim. This establishes intelligible speech, not pronunciation or sound-effect acceptance. The probe remains off the active page; the full translation is checked before being added there.

The full reference-conditioned Russian translation generated **58.29 seconds** of audio in **280.34 seconds**, averaging **589% CPU**, with **3.50 GiB peak RSS**, no sampled swap, and zero clipped samples. RTF is **4.81**, generation rate **0.321 scripted words/s**, and full-recording speaking rate **1.544 scripted words/s**. The duration estimator produced five chunks (160 forward calls), with all 13 tags preserved in the input. Different conditioning and duration estimation mean these figures do not isolate an intrinsic language-speed difference from the English voice-design run.

A separate speech pass recovered **88/90** target words. The transcript differed at `делся` (recognized as `оделся`) and `разрядился` (recognized as `разъядился`). These are flagged on the active page for listener review; ASR cannot determine whether they are pronunciation or recognition errors. Naturalness and sound rendering remain unaccepted until the listener reviews this candidate. The English recording was preserved unchanged.

## Method

- Official source `k2-fsa/OmniVoice@08be0b4ccbac3e13e374e86fbfead4b4cac343e2`; official model `k2-fsa/OmniVoice@c5fdb5ccb189668d56333f77ba2629f4cd7535f4`.
- Original F32 model and audio tokenizer, official PyTorch CPU / SDPA, six inference threads, one interop thread, no GPU inference.
- Voice-design instruction `female, young adult`, seed 1234 reset per track, 32 diffusion steps, guidance 2.0, text normalization off.
- Russian specifies `language=ru`; English specifies `language=en`. Both mixed tracks use `language=None`, the documented language-agnostic mode. They are complementary versions of the same story, not duplicate audio under two language labels.
- Default automatic long-text chunking remains enabled: 30-second activation threshold, approximately 15-second chunks. Upstream uses the first generated chunk as the voice reference for later chunks. Each chunk uses 32 diffusion steps; the entire passage can therefore involve more than 32 model forward calls.
- Four sequential fresh processes, one generation per track, no warmup and no inference batch. Model/tokenizer load is timed separately. Generate time includes all chunks, waveform decoding, and default output postprocessing. Total process time additionally includes Python imports, file writing, and process exit.
- Generation CPU time is the process user plus system CPU time delta. Average CPU percent is CPU time divided by generation wall time times 100: 100% represents one logical CPU fully busy, 600% represents six CPU equivalents. This is not the percentage of all twelve logical CPUs.
- Peak RAM is the kernel process RSS high-water mark, including model load and generation; it includes resident shared/mapped pages. Resource CSVs sample RSS, process swap, thread count, and CPU time every 100 ms during load and generation. The plots show CPU rates over approximately one-second intervals.
- Host: Ryzen 5 7600X, six physical cores / twelve logical CPUs, about 30 GiB RAM. Other applications remain running. Single-run wall times are host-contention-sensitive, not controlled repeated throughput benchmarks. Power and energy are not measured.

Each track requests all controls once, in the same order:

```text
[laughter] [sigh] [confirmation-en] [question-en] [question-ah]
[question-oh] [question-ei] [question-yi] [surprise-ah] [surprise-oh]
[surprise-wa] [surprise-yo] [dissatisfaction-hnn]
```

These are OmniVoice control names. In particular, the `en` in a non-verbal tag is not a request to switch the spoken language to English. The complete tagged text is saved in each track's request/results JSON and shown on the listening page. Tokenization is checked against the upstream recognized-tag pattern, including the order and number of tags.

## Artifacts and reproduction

The generation driver and review builder are retained in `host/benchmarks/`. Logs, input tokens, exact tagged requests, resource samples, results, and WAV files are retained under `data/benchmarks/omnivoice/`. Large runtime artifacts are ignored by git.

```sh
models/omnivoice-env/bin/python host/benchmarks/run_omnivoice.py
.venv/bin/python host/benchmarks/review_omnivoice.py
```

The review builder requires Matplotlib (installed in the project tooling environment, separate from the synthesis environment). Plots are exported as standalone SVG and PNG files beside each WAV. The initial runner writes under `data/benchmarks/omnivoice/sound-suite/`. The initial review builder restores the four-track audition; use the translation builder below for the current focused review.

```sh
# The approved-en-reference.wav crop is retained in sound-suite/.
models/omnivoice-env/bin/python host/benchmarks/run_omnivoice.py --voice-reference --probe
models/omnivoice-env/bin/python host/benchmarks/run_omnivoice.py --voice-reference
.venv/bin/python host/benchmarks/check_omnivoice_speech.py data/benchmarks/omnivoice/translation
.venv/bin/python host/benchmarks/review_omnivoice_translation.py
```

Probe/full follow-up output is saved under `translation-probe/` and `translation/`. The translation review builder requires `translation/validation.json` with both `speech_present=true` and `publishable_word_completeness=true` before exposing the new Russian WAV. That validation is separate from synthesis resource measurements; it does not grade pronunciation or effects. The checker rejects the original noise recording (0/90 target words), accepts the probe (15/15), and accepts the approved English recording (116/116 after ordinary spelling/apostrophe normalization).

Waveform validity, non-silence, output length, and clipping are checked separately from pronunciation or sound-control success. Requested tags are not automatically scored as successfully audible events. This is a listening audition; naturalness, unwanted noises, and event rendering require listener evaluation.

The production service configuration and TTS backend are unchanged.

Upstream references: [generation API](../models/omnivoice-src/omnivoice/models/omnivoice.py), [generation parameters](../models/omnivoice-src/docs/generation-parameters.md), [non-verbal controls](https://github.com/k2-fsa/OmniVoice/tree/08be0b4ccbac3e13e374e86fbfead4b4cac343e2#non-verbal--pronunciation-control).
