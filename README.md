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
   - `bin/pocket-tts`, plus `models/ru/{model_fast,voice}.safetensors` and `models/ru/tokenizer.model` for Russian
   - captures from your own Nest: `data/native-template.bin`, `data/native-nospeech.bin`, `data/offline-account-ids.json`
2. `cp config.env.example config.env`, then set the Nest and network values.
3. `./run.sh` installs and starts the systemd service (needs sudo).
4. Replug the Nest.

`./run.sh restart|stop|logs|uninstall` · tasks: http://127.0.0.1:53412 · tests: `.venv/bin/python -m unittest discover -s tests`

The monitor’s **Test** button creates a browser test pipeline. Click the microphone
to record, then stop to transcribe and generate a reply locally. At the final step,
choose **Cast to Nest** or **Play** in the browser. Microphone recording requires
localhost or HTTPS; recordings stop automatically after 60 seconds.

For offline Cast playback, run `bash host/setup-cast.sh` once to cache the public
receiver SDK, then restart. The proxy serves a local WAV receiver at the Nest's
default receiver URL; other Google requests stay blocked. No receiver assets
are downloaded at runtime.

To try KittenTTS 2 (1.7B) on the CPU, run `./host/setup-kitten.sh`, set
`TTS_BACKEND=kitten` in `config.env`, and run `./run.sh restart`. English uses
Bruno and Russian uses the Russian voice. Each TTS step shows its generation
time and peak RAM. Set `TTS_BACKEND=pocket` to switch back. The Kitten runtime
uses cached model files only; it releases the model after each request and
queues simultaneous TTS requests. See [measurements](docs/kitten-tts-2.md).

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
