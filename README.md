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

License: GPL-3.0, see `LICENSE`.
