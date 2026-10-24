# Kitten TTS control correctness audit — 2026-10-23

This follows listener reports that English vocal events were inaudible and emotion controls were weak in both languages. The voice-variation sweep was stopped. This audit holds the original Bruno/Russian voices, text, full decoder, expressive preset and seed constant. It tests implementation correctness before interpreting delivery quality or repeating performance benchmarks.

## Confirmed defect and local fix

**audio.cpp exempted codec token 4299 (silence) from its repetition penalty. The upstream Kitten implementation deliberately penalizes this token to prevent silence loops.** A compiled regression check extracts and executes the actual native penalty block and compares its output with upstream `RepetitionPenaltyExempt`. Positive silence logits, negative silence logits and repeated silence all failed before the patch; all six cases now match exactly. End-of-sequence behavior is unaffected because termination tokens are outside the generated-code history.

- [Patch](../host/patches/audio-cpp-kitten-silence-penalty.patch)
- [Executable reference comparison](../host/check-audio-cpp-sampling.py)
- [Before](../data/benchmarks/tts-correctness/sampling-before.json) / [after](../data/benchmarks/tts-correctness/sampling-final.json) results

The patch is applied to the local audio.cpp checkout and the CLI has been rebuilt. Temporary diagnostic hooks were removed. The running Nest service still selects the installed `kitten` backend; this does not deploy audio.cpp or establish that this sampling correction cures missing vocal events.

## Checks completed

| Check | Result |
|---|---|
| LM weights vs original BF16 checkpoint | All 314 tensors exactly equal after Q8 unpacking |
| Decoder weights vs official S3 MeanFlow checkpoint | All 1210 tensors byte-exact, with GGUF singleton-dimension normalization |
| Tokenizer files and LM config | Byte-identical |
| Prompt construction / tag tokenization | Exact matches in all eight English/Russian cases |
| Reference transcript and codec tokens | Exact for Bruno and Russian |
| Speaker projection | Exact match to the upstream BF16 projection for both voices |
| Initial LM logits | Same top token in all eight cases; max absolute error 0.193203, below upstream test limit 0.5 |
| Installed kitten-tts-cpp English normalizer | Preserves emotion, sound and emphasis markup, including in actual generation reports |
| Native decoder vs published TorchScript components | Both languages pass encoder, meanflow mel, F0 and HiFT comparisons using fixed speech codes and zero Gaussian noise |
| Listening files | 24 valid mono 24 kHz PCM16 WAVs; same-code decodes have identical frame counts |

The packaged top-level config has different byte-size entries for prepared voice files in its `cpp` manifest. Generation settings, control prefix and token map are equal. This metadata difference does not alter the tested prompts.

Numerical decoder maxima:

| Voice conditioning | Encoder | Mel | F0 | Waveform |
|---|---:|---:|---:|---:|
| Bruno / English laugh codes | 1.43e-6 | 6.51e-4 | 4.17e-7 | 3.97e-5 |
| Russian / Russian laugh codes | 1.91e-6 | 8.66e-4 | 3.58e-7 | 1.02e-4 |

The waveform comparison excludes the first 960 fade samples as the upstream component test specifies. These component tests control Gaussian noise to isolate arithmetic; ordinary generated WAVs use each runtime’s normal stochastic decoder. Small logit differences and different RNG implementations can still produce different sampled speech despite identical seeds.

## Controlled listening comparison

- [Open the three-path gallery](../data/benchmarks/tts-correctness/index.html)
- [Download all comparison clips](../data/benchmarks/tts-correctness/comparison.zip)

For every case the gallery provides:

1. **Upstream LM → upstream decoder**: the unmodified upstream `_generate_tokens` method, prompt builder, logits processors and speaker hook, using the original BF16 checkpoint. The method is loaded independently of the unrelated cloning/STT initializer. The published upstream TorchScript decoder converts its speech codes to WAV.
2. **audio.cpp LM → upstream decoder**: exactly the codes captured from the patched native runtime, decoded with the same TorchScript module, prepared conditioning and reset decoder seed as the first path.
3. **audio.cpp LM → audio.cpp decoder**: the native WAV from those same captured codes and conditioning.

Plain speech, `<laugh>`, `[joyful]` and emphasis are tested in both languages without changing voice or wording. This is a focused regression set, not a perceptual score for every supported event. The previous 46 clips retain the broader event coverage.

**What is established:** markup is not stripped, the expression-control prefix is present, the sampled model uses the expected weights and speaker conditioning, and the waveform stages agree numerically. One native sampling discrepancy was reproduced and corrected.

**Listener review of the matched clips:** English works in all compared paths. Russian laughter is rendered only by the upstream token generator; neither decoding of native speech tokens restores it. Russian pronunciation is unnatural in all three paths, including upstream. The pronunciation problem is therefore reproduced independently of the native port with this model/voice. The native laughter difference remains a generation/sampling issue rather than waveform decoding. This does not establish a general success rate across all tags or seeds.

## Reproducibility

- Reference model: `KittenML/kitten-tts-2@1f00fee525c7b2e30bbe9ca9ba67529734fce9c9`.
- Reference source: `KittenML/KittenTTS@afd9dfda7ffdca35dd0151b149c221140c80ec3f`.
- Native source: `0xShug0/audio.cpp@c8365f391307a97bfe133e0872e632fe572ecace`, plus the one-line patch linked above.
- Reference tools: CPU Torch 2.13.0 and Transformers 5.20.2, in `models/kitten-reference-env`. The same checkpoint’s published default TorchScript decoder is used for common-decoder audio.
- Asset hashes, captured prompt tokens, initial logits, speech codes, test scripts, logs and JSON reports are under `data/benchmarks/tts-correctness/`. Full reference assets are under `models/kitten-reference-assets/`. These runtime artifacts are ignored by git.

```sh
# Regression check; returns nonzero for the original silence exemption:
models/kitten-reference-env/bin/python host/check-audio-cpp-sampling.py sampling-final.json
# Apply to a clean checkout at the pinned native commit:
git -C models/audio-cpp-src apply ../../host/patches/audio-cpp-kitten-silence-penalty.patch
models/audio-cpp-tools/bin/cmake --build models/audio-cpp-src/build-kitten2 --target audiocpp_cli --parallel 4
```

Powered by Stellon Labs. [Upstream sampler](https://github.com/KittenML/KittenTTS/blob/afd9dfda7ffdca35dd0151b149c221140c80ec3f/kittenml/kittentts2/logits.py).

## Continued audit and fixed-voice benchmark

The installed `kitten-tts-cpp` runtime was also checked through its actual generation reports: both English and Russian prompts exactly match the upstream prompt builder, including `{emo: 1}`. The English normalizer preserves markup in the actual target text.

Teacher-forcing identical native speech prefixes through the upstream F32 model compared **421 decoding steps**, with **416/421** top-token matches. Maximum raw logit error was **0.246655**. Native temperature, top-k and top-p transformations produced exactly matching finite-token masks and values for the captured logits. Per-case mean total variation between the native/reference sampled distributions ranged from 0.0064 to 0.0097. These small differences can still change sampled sequences; they are not proof of equal control-following quality.

A focused CPU sampling experiment preserves upstream full-vocabulary random coordinates rather than Kitten's current compact-vocabulary `std::discrete_distribution` path. Given identical captured logits, the experiment matches eight successive upstream PyTorch token selections exactly. Applied to the actual model, numerical differences still make complete speech-token sequences diverge, so an identical seed is not an identical clip. Listener review found that Russian laughter is still missing with this experiment. It is therefore rejected as a fix and is not enabled in the application. Temporary sampling instrumentation was removed; the native checkout retains only the tested silence-penalty correction.

- [Benchmark protocol](../data/benchmarks/tts-correctness/BENCHMARK_PROTOCOL.md)
- [All controls with matched reference/native players](../data/benchmarks/tts-correctness/controls.html)
- [Download control benchmark](../data/benchmarks/tts-correctness/control-benchmark.zip)
- [Recorded listener feedback](../data/benchmarks/tts-correctness/listener-feedback.json)
- [Decoder-step numerical comparisons](../data/benchmarks/tts-correctness/decode-step-parity.json)
- [Exact sampling-warper comparisons](../data/benchmarks/tts-correctness/warper-parity.json)
- [Original Russian voice conditioning recording](../data/benchmarks/tts-correctness/voice-references/ru_russian.wav)

The control set holds the checkpoint, original voices, sentences and expressive preset fixed. It includes all ten sound tags, plain/emotion/emphasis controls, and seed 42 repeats of plain/laugh/joyful. Effects and pronunciation are scored separately by listening; no automatic emotion or event scores are inferred from transcription, durations or waveform differences.

The expanded control suite contains 34 reference clips, 34 native clips, and six experimental-sampling clips (74 total). File and archive integrity were verified. These tests establish valid outputs, not successful perceptual control following.

## Precision listening result and pronunciation follow-up

The listener confirmed that the upstream BF16 and F32 Russian clips both render laughter, and both pronounce the words unnaturally (reported as «ну щьто, нашьнем»). Precision alone therefore does not explain the missing native laugh in that comparison. The sampling-RNG experiment was also rejected by listening. No sampling or precision change has been promoted as a control-following fix.

The active HTML review now contains only the current targeted pair; broad diagnostic galleries are archived separately. The next controlled pronunciation check keeps the upstream BF16 model, five-sentence passage, expressive preset and seed fixed and compares `Russian` with `RussianLiteratureTeacher`. The passage includes `<um>`, `<gasp>`, `<laugh>` and `<sigh>`. Its two clips require listening review before selecting any new production voice.

## Acceptance result: longer Russian passage

**Failed.** The listener reported that both upstream BF16 voices completely skipped the sound tags in the five-sentence passage. `RussianLiteratureTeacher` sounded better than `Russian`, but its pronunciation remained notably imperfect. Neither voice passes the tested requirements.

This test uses the same upstream reference generation path, correct markup and expression prefix, expressive preset, original BF16 weights and full decoder. It reproduces unreliable event rendering and imperfect Russian pronunciation independently of audio.cpp. Successful short laughter samples and numerical runtime parity do not establish reliable control following over normal assistant replies.

The teacher preset is a relative improvement, not an accepted fix. No production voice, sampling method, precision or backend was switched as a result of this audition. The verified native silence-penalty patch remains a separate correctness fix and is not presented as a remedy for these quality failures.

| Acceptance criterion | Listener result |
|---|---|
| Render the requested vocal events in the longer passage | FAIL for both voices |
| Natural Russian pronunciation | FAIL for both voices |
| Teacher preset improves pronunciation relative to current preset | YES, but still imperfect |

The completed benchmark therefore rejects this tested Kitten configuration for reliable expressive Russian replies. Further work needs a different voice/model capability assessment or an explicit audio-event playback design; more temperature/preset tuning is not an established fix.
