# OmniVoice Russian CPU audition — 2026-10-23

Tested the official upstream implementation after the Kitten Russian control-following and pronunciation failures. This is a first audition, not a production integration or a completed perceptual evaluation.

- [Current focused listening page](../data/benchmarks/tts-correctness/index.html): the approved English recording and a fully Russian translation. The initial four-track audition and listener feedback are retained in [the sound-suite report](omnivoice-sound-suite.md).
- [Archived first comparison](../data/benchmarks/tts-correctness/index-before-omnivoice-sound-suite.html): the original OmniVoice versus Kitten Russian literature teacher audition.
- [OmniVoice WAV](../data/benchmarks/omnivoice/ru-omnivoice.wav)
- [Request and measured results](../data/benchmarks/omnivoice/results.json)

## Pinned configuration

- Source: `k2-fsa/OmniVoice@08be0b4ccbac3e13e374e86fbfead4b4cac343e2`.
- Model: `k2-fsa/OmniVoice@c5fdb5ccb189668d56333f77ba2629f4cd7535f4`. All downloaded file sizes and LFS SHA-256 values verified; see `models/omnivoice/download.json`.
- Official PyTorch CPU inference, six threads, original F32 model weights and F32 Higgs audio tokenizer.
- Torch `2.11.0+cpu`, torchaudio `2.11.0+cpu`, Transformers `5.20.2` in a separate `models/omnivoice-env` environment.
- Default 32 diffusion steps; guidance 2.0; seed 1234; `language=ru`; text normalization off.
- Voice-design instruction: `female, young adult`. No reference-audio cloning and no ASR model loaded.
- Source repository unchanged. A forward hook printed progress; it did not modify inference outputs. Runtime model loading was forced offline against the verified local assets.

## Passage and vocal controls

```text
Ну что, начнём. [confirmation-en] У меня для тебя хорошая новость. [surprise-ah] Неужели у нас всё получилось? [laughter] Как здорово! [sigh] Теперь можно немного отдохнуть.
```

OmniVoice uses its own supported syntax: `[confirmation-en]`, `[surprise-ah]`, `[laughter]`, `[sigh]`. Kitten’s `<um>`, `<gasp>`, `<laugh>`, `<sigh>` and `[joyful]` controls were not sent to OmniVoice. The spoken sentence content matches the previous five-sentence passage. Controls and voices differ, so this is a model audition rather than a test that isolates only model weights.

## Measurements

| Measurement | First loaded-process generation |
|---|---:|
| Load model and audio tokenizer | 0.70 s |
| Generate and decode | 37.99 s |
| Output duration | 10.56 s |
| RTF (lower is faster) | 3.60 |
| Peak process RSS | 2.75 GiB |
| Average CPU equivalents during generation | 5.89 |

This single run was outside the service cgroup on the shared Ryzen 5 7600X machine. It is not a warm-throughput benchmark. The generated file was checked for finite, non-silent output and valid mono 24 kHz PCM16 WAV data. A local Whisper pass checks word completeness only; its transcription is not evidence that the effects were rendered or pronunciation was natural.

Listener feedback: OmniVoice sounds much more natural than the previous Kitten samples, but includes some unexpected vocal sounds. The first sample explicitly requested four non-verbal events, so it does not isolate unsolicited sounds from requested effects. The follow-up [four-track audition](omnivoice-sound-suite.md) expands the passage, requests all 13 supported tags, and records per-track CPU and memory consumption. This does not establish that all sound controls work reliably. The production service has not been restarted or changed to OmniVoice.

## Reproduce

```sh
models/omnivoice-env/bin/python data/benchmarks/omnivoice/run.py
.venv/bin/python data/benchmarks/omnivoice/write_review.py
```

Run scripts, logs, configuration, tag-token IDs, initial input tokens and results are retained under `data/benchmarks/omnivoice/`; source/environment/model assets are under `models/omnivoice*`. These large runtime artifacts are ignored by git.

Upstream references: [repository](https://github.com/k2-fsa/OmniVoice/tree/08be0b4ccbac3e13e374e86fbfead4b4cac343e2), [generation parameters](https://github.com/k2-fsa/OmniVoice/blob/08be0b4ccbac3e13e374e86fbfead4b4cac343e2/docs/generation-parameters.md), [model card](https://huggingface.co/k2-fsa/OmniVoice/blob/c5fdb5ccb189668d56333f77ba2629f4cd7535f4/README.md). Source license: Apache-2.0. The model card states CC-BY-NC for pretrained weights.
