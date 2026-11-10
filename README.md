# local-nest-mini

Managed by `~/Projects/Misc/local-infra/`, under `services/local-nest-mini/`.
The former `~/Projects/Hardware/local-nest-mini` path is a compatibility symlink.
The shared HTTP gateway routes this Nest’s port 80 traffic to the existing local
proxy on 18081; the helper retains its device-only port 443 bridge and DNS/ARP/NDP
rules. Its `--shared-http` flag prevents a competing port 80 listener.
Monitor: **http://kitkat-pc/nest/**. Port 80 ownership and startup live in
local-infra; `local-nest-mini.service` requires `local-infra-gateway.service`.

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

## Setup (local-infra / Podman)

The application runs as three rootless Podman containers in local-infra's
**top-level `compose.yml`**: `nest-assistant` (including the monitor), `nest-s3`
(native TLS/HTTP2 voice endpoint), and `nest-proxy`. Each uses host networking
so Cast multicast, LAN audio downloads, the host gateway and loopback connections
keep their existing addresses. The image owns Python and speech dependencies;
models, device captures and configuration are mounted from this checkout.
The small root `local-nest-network.service` runs only the standard-library
DNS/ARP/NDP/firewall helper on the host. It has no dependency on `.venv` or Torch.

1. Retain `config.env`, `.env`, `proxy-config/`, `data/native-template.bin`,
   `data/native-nospeech.bin`, `data/offline-account-ids.json`, `models/stt/futo.bin`
   and the five speech assets in `models/fast-tts/`. These private local assets
   are not included in Git or the image build context. Keep `PC_IP` and
   `PUBLIC_BASE_URL` reachable from the Nest (currently port 8099 on the PC's LAN IP).
2. From local-infra run `bash scripts/install-nest.sh`. It builds the runtime
   image, starts the top-level Compose project and enables the user unit
   `local-infra-compose.service`. The Compose provider uses a managed Python;
   on a different machine set `COMPOSE_PYTHON` to a stable Python 3.12+ interpreter
   before running `bash scripts/setup-compose.sh`.
3. Once the containers are healthy, run `sudo bash scripts/install-nest-network.sh`
   from local-infra. This verifies the endpoints, disables the old
   `local-nest-mini.service`, and enables the host-only network helper.
   If the old service is still running, stop it before step 2 to release its ports.
4. Open **http://kitkat-pc/nest/**. The Nest should reconnect automatically;
   replug it if it retains a failed connection from the migration.

From local-infra: `podman compose ps`, `podman compose logs -f`, or
`./scripts/compose.sh up -d --build`. `make start|stop|restart|status` includes
the Compose user unit; `make nest-logs` follows the Nest containers. Building
`nest-assistant` also builds the shared image for the proxy and S3 services.
A healthy S3 dependency is checked with TLS and HTTP/2 before the proxy starts.
The existing gateway continues to own port 80 and forward `/nest/` to 53412.

The `runtime` image target uses Silero/Pocket CPU speech. The `omnivoice` target
retains the optional legacy backend; it is not built or selected by this stack.
Legacy host setup scripts and `run.sh` remain for standalone installs; do not
start that full host supervisor alongside the Compose stack.

Tests: `.venv/bin/python -m unittest discover -s tests`. The host `.venv` is
optional for development and is no longer used by the running assistant.

The Nest can be offline when the service starts. The helper discovers its address
by `NEST_MAC` using the interface's neighbor cache and observed ARP/IPv4 traffic,
and sends Cast mDNS discovery queries while it is missing. It verifies the MAC
reported by the device every five seconds before enabling redirection. `NEST_IP`
is an optional initial hint and can be empty. Address changes update a temporary
nft set and `data/nest-address.json`; the shared gateway reads that verified lease
for each connection. Both expire after twenty seconds without refresh. Firewall
permissions match the Nest's MAC rather than its changing IP. Reconnects and DHCP
changes need no configuration edits or service restarts; the assistant and proxy
stay running. A different device claiming an old IP fails identity verification.

Tasks: http://127.0.0.1:53412 (also available through the shared `/nest/` gateway).

The monitor’s **Test** button creates a browser test pipeline. Click the microphone
to record, then stop to transcribe and generate a reply. At the final step,
choose **Cast to Nest** or **Play** in the browser. Microphone recording requires
localhost or HTTPS; recordings stop automatically after 60 seconds.
Each pipeline step shows its elapsed time, updating while it runs.

The monitor's **Stop assistant** button keeps network interception running and
immediately answers each new Nest voice request with an empty native reply,
without STT, LLM generation or TTS. **Resume assistant** restores normal replies.
This mode is shared by all monitor tabs and survives service restarts. It does
not clear conversation history or affect browser test pipelines. **Pause updates**
only pauses the monitor display.

Follow-up turns without a fresh “Hey Google” are disabled. The local S3 endpoint
returns the empty native reply as soon as query audio arrives without preceding
hotword audio, while the upload is still open. Such turns skip speech detection,
STT, LLM generation, TTS and Cast. Start each new request with “Hey Google”.

The processor sends the transcript and Laylo's voice instructions to the configured
API, then passes the assistant's reply to local TTS
and the selected playback transport. All Nest, HTTP and browser test requests
share one conversation, retaining the latest 50 user/assistant messages (25 full
exchanges). Older messages are dropped as new ones arrive; each API request
includes at most 50 conversation messages plus the system instructions. Concurrent
turns are processed in order. History lives in memory and resets on service
restart. Failed requests and spoken error messages are not saved in history. STT
and TTS remain local; transcripts go to your configured LLM provider. If generation
fails, the reply becomes “Не получилось сгенерировать ответ. Диктую ошибку: …”
with the error message, and continues through TTS and playback.

`LLM_API=auto` (default) uses `/responses` on `api.openai.com` and
`/chat/completions` on other hosts. Set `LLM_API=responses` or
`LLM_API=chat_completions` to override this for another provider. OpenAI's
`gpt-6.1-sol` rejects function tools with reasoning on Chat Completions, so it
uses Responses. Responses requests set `store=false` and carry reasoning and
tool output items through each tool round within the response. Final text and
sentence streaming use the same history and playback paths with either API.

Set `KEENABLE_API_KEY` in `.env` to enable Keenable `web_search` and
`read_web_page`, matching the web tools in `context-tg`. The assistant can search
for current facts and read relevant sources before giving a spoken answer.
Each response has a hard limit of **five tool calls total**, including page
reads, failed calls and calls in the same batch. Once exhausted, the model gets
one final request with tool use disabled. The next user message starts a fresh
allowance. Tool results are limited to 16 KB each and stay within the current
response; the shared 50-message history retains user messages and final replies.

Set `LLM_STREAMING=1` in `.env` to stream response text and start speech before
the whole reply is generated. With `OUTPUT_MODE=native`, the model is instructed
to put each sentence on its own line and end it with `.`, `!`, or `?`. Completed
lines are synthesized as they arrive. Silero and Pocket stay loaded in a shared
CPU worker across sentences and requests; each sentence becomes a WAV clip.
The optional OmniVoice GPU backend uses its native `--stream-by-line -o -`
mode, with per-sentence CPU inference as fallback after a native GPU failure.
Malformed lines fall back to splitting on sentence punctuation; a final line
without a newline is flushed at the end of the text stream.

The first sentence is returned through the normal Nest S3 response. Once its
delivery is acknowledged, later sentences play through Cast in order. The
handoff waits for the first clip's duration plus a short playback guard; this
is an estimate, not a device playback-completion acknowledgement. Each Cast
clip finishes before the next starts. Synthesis runs ahead with bounded queues.
This requires `PLAYER_ENABLED=1` and the offline Cast receiver assets from
`host/setup-cast.sh`. Interrupting the task stops queued playback, and a new
streaming turn supersedes the previous turn's remaining speech. History saves
the complete final response, and the five-tool budget still applies to the
whole response. Browser Test pipelines keep their explicit playback controls.

Research: the [chat streaming API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
emits text deltas rather than sentence events. [OmniVoice's native CLI](https://github.com/ServeurpersoCom/omnivoice.cpp/blob/master/docs/ARCHITECTURE.md)
supports incremental stdin and separate WAV headers per input line, which this
optional OmniVoice path uses.

LLM and local assistant HTTP requests allow five minutes (300 seconds) per
request; `/answer` includes LLM generation and TTS (up to the first audio clip
in streaming mode). Whisper inference also allows five minutes. OmniVoice retains its internal
ten-minute limit. S3 heartbeats continue during processing, but these timeouts
do not override any deadline enforced by the Nest itself.

For offline Cast playback, run `bash host/setup-cast.sh` once to cache the public
receiver SDK, then restart. The proxy serves a local WAV receiver at the Nest's
default receiver URL; other Google requests stay blocked. No receiver assets
are downloaded at runtime.

**Silero v5.6 / Xenia** for Russian and **Pocket TTS 2M / Alba** for English are
the default (`TTS_BACKEND=silero_pocket`). Run `bash host/setup-fast-tts.sh` to
prepare the separate CPU environment and local assets, then restart the service.
Both models warm up at startup and remain resident across requests, using two
CPU threads by default. `FAST_TTS_THREADS`, `FAST_TTS_PYTHON`, and `FAST_TTS_DIR`
override the thread count, interpreter, and asset directory. Requests are
serialized; cancellation stops the active worker and the next request starts
a fresh one. Sentence streaming uses the same resident worker. Runtime inference
is offline and uses no GPU. Each TTS task reports the selected engine and timing.

These models do not reliably handle mixed-language text. The LLM is instructed
to use only the request's language and script, translating or transliterating
foreign terms, names, abbreviations and letters (for example, `Хоуминг` in a
Russian reply). This is a generation instruction, not a hard output filter.

**OmniVoice** remains an optional backend for English and Russian, with 24 refinement steps.
Each reply first checks free VRAM on the discrete GPU (two amdgpu sysfs reads).
With more than 1.7 GiB free it runs
[omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp) on Vulkan with a
Q8_0 transformer and F32 codec: about 2.3 s for a 5.7 s Russian reply, peaking at
1.3–1.6 GiB VRAM, released when the reply is done. Otherwise, or if the GPU run
fails, it uses the optimized PyTorch CPU path (six threads, BF16 transformer, FP32
codec): about 13 s for the same reply and 5 GiB RAM. Both clone the saved
synthetic English voice reference, so their voices match closely but not
bit-for-bit. Text normalization is disabled; Laylo writes numbers as spoken words.
The LLM prompt asks for plain speech, and square-bracket sound tags are removed
before synthesis. Each TTS task reports the engine, timing, and VRAM or RAM; a CPU
run also records why the GPU was skipped. Requests are serialized. Runtime
inference is offline.

Legacy setup: `bash host/setup-omnivoice.sh` (CPU path), then optionally
`bash host/setup-omnivoice-vulkan.sh` inside the build toolbox (GPU path). Select
`TTS_BACKEND=omnivoice` in `config.env` and run **`./run.sh install`**; the unit
has `MemoryHigh=6G` and `MemoryMax=8G` for the CPU path's ~5.2 GiB peak. Settings:
`OMNIVOICE_GPU=auto|off`, `OMNIVOICE_GPU_MIN_FREE_MIB=1740`,
`OMNIVOICE_GPU_STEPS=24`, `OMNIVOICE_GPU_DEVICE=Vulkan0`, `OMNIVOICE_THREADS=6`,
`OMNIVOICE_STEPS_EN=24`, `OMNIVOICE_STEPS_RU=24`. Paths can be overridden with
`OMNIVOICE_PYTHON`, `OMNIVOICE_DIR`, `OMNIVOICE_REFERENCE`, `OMNIVOICE_GPU_BIN`,
`OMNIVOICE_GPU_MODEL`, and `OMNIVOICE_GPU_CODEC`.

Local-infra Compose builds the Silero/Pocket inference environment and mounts local
model assets read-only, with an 8 GiB assistant limit. The optional `omnivoice`
image target is separate from the default runtime.

The optional OmniVoice CPU weights and environment remain because
they provide the fallback when the GPU is busy.

License: GPL-3.0, see `LICENSE`.
