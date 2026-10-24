# local-nest-mini

A Nest Mini answered by this PC instead of Google. Nothing is forwarded to Google.

```
Nest Mini
  │  DNS + ARP/NDP redirected to the PC (nest_network_helper.py)
  ▼
mitmproxy + nest_relay.py   local token and connectivity replies, other Google traffic blocked
  │  assistant-s3-pa.googleapis.com
  ▼
app/s3_server.py            mic audio in, end of speech detected, native reply out
  │
  ▼
app/server.py               Whisper STT → app/processor.py → TTS
```

## Setup

1. Put the files that are not in git in place:
   - `bin/whisper-cli` (static whisper.cpp) and `models/stt/futo.bin`
   - OmniVoice CPU environment, pinned model and voice reference: run `bash host/setup-omnivoice.sh`
   - captures from your own Nest: `data/native-template.bin`, `data/native-nospeech.bin`, `data/offline-account-ids.json`
2. `cp config.env.example config.env`, then set the Nest and network values.
   Copy `.env.example` to `.env` and set `LLM_BASE_URL` (including `/v1` when
   required by your provider) and `LLM_MODEL`. `LLM_API_KEY` and
   `LLM_REASONING_EFFORT` are optional. These variables can also live in
   `config.env`; `.env` takes precedence in the host service and Compose.
3. `./run.sh` installs and starts the systemd service (needs sudo).
4. Replug the Nest.

The host needs Bash, Python 3 with `venv`/pip, ffmpeg, and systemd. Install
dependencies and generate the proxy CA with `bash host/setup-host.sh` (also run
automatically by `./run.sh install`). Models, binaries, credentials, certificates,
and owned-device captures are private local assets and are ignored by git;
they are not shipped with this repository. Complete steps 1–2 on a new checkout.
Installation checks the runtime prerequisites before replacing the service unit.

After moving the folder on the same host, run `./run.sh install` from its new
location. Installation renders the systemd paths from that checkout and repairs
the OmniVoice source link. Python tools run through their interpreter so moved
virtual environments do not rely on old console-script paths. The tracked unit
is a template; install it through `run.sh`. A different machine or Python version
may require recreating the ignored Python environments and rerunning setup.
`bash host/service-run.sh --check` checks prerequisites without starting services.

The Nest can be offline when the service starts. The helper checks its configured
IP and MAC every five seconds, waits through disconnects, and reapplies network
redirection when it reconnects. The assistant and proxy stay running. A different
device at that IP is not redirected; update `NEST_IP` if the Nest's address changes.

`./run.sh restart|stop|logs|uninstall` · tasks: http://127.0.0.1:53412 · tests: `.venv/bin/python -m unittest discover -s tests`

The monitor’s **Test** button creates a browser test pipeline. Click the microphone
to record, then stop to transcribe and generate a reply. At the final step,
choose **Cast to Nest** or **Play** in the browser. Microphone recording requires
localhost or HTTPS; recordings stop automatically after 60 seconds.

The processor sends the transcript and Laylo's voice instructions to the configured
API's `/chat/completions` endpoint, then passes the assistant's reply to local TTS
and the selected playback transport. Each request is a fresh conversation. STT
and TTS remain local; transcripts go to your configured LLM provider. API failures
fail the processing stage without generating fallback speech.

LLM and local assistant HTTP requests allow five minutes (300 seconds) per
request; `/answer` includes both LLM generation and TTS. Whisper, Kitten and
Pocket inference also allow five minutes. OmniVoice retains its longer internal
ten-minute limit. S3 heartbeats continue during processing, but these timeouts
do not override any deadline enforced by the Nest itself.

For offline Cast playback, run `bash host/setup-cast.sh` once to cache the public
receiver SDK, then restart. The proxy serves a local WAV receiver at the Nest's
default receiver URL; other Google requests stay blocked. No receiver assets
are downloaded at runtime.

Optimized **OmniVoice** is the default for English and Russian. It runs on CPU
with six threads, a BF16 transformer and an FP32 audio codec. English uses
16 refinement steps; Russian uses 32. Both use the saved synthetic English voice
reference to avoid unstable voice-design generations. Text normalization is
disabled; Laylo writes numbers as spoken words.
The LLM prompt uses OmniVoice's native square-bracket sound tags. Each TTS task
reports generation time, CPU usage and peak RAM. Requests are serialized, and
the worker releases the model after each request. Runtime inference is offline.

For this upgrade, run `bash host/setup-omnivoice.sh`, select
`TTS_BACKEND=omnivoice` in `config.env`, then run **`./run.sh install`** to install
the updated memory limits and restart. A restart alone does not update the unit:
the service now has `MemoryHigh=6G` and `MemoryMax=8G`, because measured OmniVoice
peak RSS reaches about 5.2 GiB. Model settings are `OMNIVOICE_THREADS=6`,
`OMNIVOICE_STEPS_EN=16`, and `OMNIVOICE_STEPS_RU=32`; paths can be overridden with
`OMNIVOICE_PYTHON`, `OMNIVOICE_DIR`, and `OMNIVOICE_REFERENCE`.

Compose builds the separate CPU inference environment and mounts local model/voice
assets read-only, with an 8 GiB assistant limit. Run the setup script before
building/starting Compose. The host service uses its existing model environment.

Legacy `kitten`, `kitten-mini`, and `pocket` backends remain explicitly selectable.
Kitten setup is `./host/setup-kitten.sh`; its full model uses `expressive` and
disables normalization for Russian. See [Kitten measurements](docs/kitten-tts-2.md).

The [audio.cpp CPU trial](docs/audio-cpp-kitten-tts-2.md) compares the full
Kitten TTS 2 model with the current runtime and links English/Russian emotion
and sound samples saved locally.

The [OmniVoice CPU optimization report](docs/omnivoice-cpu-optimization.md)
records English/Russian speedups, CPU/RAM measurements and the focused listening
comparison. The selected settings are now wired into the processor.

The faster English option is **Kitten TTS Mini 80M**: run
`./host/setup-kitten.sh mini`, set `TTS_BACKEND=kitten-mini`, then restart.
It uses about 276–310 MiB RAM and generates the short test reply in about
0.3 seconds (about 0.85 seconds including loading). Mini uses English phonemes;
Russian requests continue through Pocket TTS. See [Mini measurements](docs/kitten-tts-mini.md).

Powered by KittenML when a Kitten backend is selected. Kitten TTS 2 weights
are separately licensed under the
[Stellon Labs Community License](https://huggingface.co/KittenML/kitten-tts-2/blob/main/LICENSE.md).
Mini 0.8 is [Apache-2.0](https://huggingface.co/KittenML/kitten-tts-mini-0.8).

License: GPL-3.0, see `LICENSE`.
