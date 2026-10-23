# KittenTTS 2 CPU trial — 2026-10-23

Measured on this Nest server: AMD Ryzen 5 7600X, 6 physical cores / 12 logical
CPUs, approximately 30 GiB RAM. No GPU was used. These are local measurements,
not the publisher's advertised performance.

The installed backend is `kitten-tts-cpp==0.2.0`, with CPU-only
`torch==2.13.0+cpu`, in `models/kitten-env`. The model is
`KittenML/kitten-tts-2`, pinned to revision
`1f00fee525c7b2e30bbe9ca9ba67529734fce9c9`, using the losslessly packed
1.7B TQ2_1 model and the full default decoder. Generation uses the stable
preset and seed 1234. The smaller student decoder was not selected.

| Sample | Model load + imports | Synthesis | Audio duration | Peak process RAM | Mean CPU during synthesis |
| --- | ---: | ---: | ---: | ---: | ---: |
| English, 6 LM threads, first run | 2.32 s | 15.38 s | 3.62 s | 2268 MiB | 5.11 logical CPU equivalents |
| English, 6 LM threads, second run in same process | already loaded | 15.58 s | 3.62 s | 2351 MiB | 5.09 |
| English, 8 LM threads | 2.39 s | 15.41 s | 3.62 s | 2265 MiB | 6.69 |
| Russian, 6 LM threads | 2.27 s | 23.63 s | 7.00 s | 2318 MiB | 5.20 |

The decoder uses two threads in all cases. The English input is
`I have heard: Hello from our local Nest Mini.` The Russian input is
`I have heard: Привет, это проверка нашего локального голосового помощника.`
The processor's existing English prefix remains intact.

Six LM threads were retained because eight consumed more CPU without making
this sample faster. Synthesis is approximately 3.4–4.3 times slower than real
time on these samples. With imports/model loading, expect about 18 seconds for
the English sample and 26 seconds for the Russian sample, before STT or Nest
transport time. Keeping the model loaded did not materially improve synthesis
speed in this test, although it avoids about 2.3 seconds of loading per call.

Peak RAM is Linux `ru_maxrss` for the Kitten worker, including Python, LibTorch,
weights, decoder, and generation buffers. CPU is the change in user + system
CPU seconds divided by synthesis wall time; 5.1 means approximately 510% in
`top`, or 43% of this machine's 12 logical CPUs. Independent `/usr/bin/time -v`
measurements agreed with the worker's peak RAM. Neither measured run swapped.
No power or GPU telemetry was inferred from CPU usage.

Prepared model assets occupy about 1.6 GiB, and the isolated Python environment
about 1.1 GiB. The model's packed GGUF is 1,029,076,832 bytes; the decoder and
voice-conditioning assets are additional downloads.

The active service's 3 GiB hard memory limit and 2 GiB memory high-water setting
are unchanged. The measurements above ran outside its cgroup, so memory
pressure inside the service can affect latency. The worker alone approaches
the high-water setting; the remaining service and overlapping STT add memory.
TTS jobs are serialized to prevent simultaneous copies, but this is not a
guarantee that every long or concurrent workload fits the service limit.

The integration uses a separate cancellable process per request. Its model is
released afterward, so it adds no permanently resident model memory to the
idle server. Inference runs offline against the prepared cache. The TTS step
in the Test UI identifies Kitten and displays the measured total load +
synthesis time, audio duration, and worker peak RAM.

Raw JSON reports, `/usr/bin/time` logs, and generated sample WAV files are in
`data/benchmarks/kitten-tts-2/` (ignored by git). Reproduce a two-run test with:

```sh
models/kitten-env/bin/python app/kitten_tts.py \
  --cache-dir models/kitten-tts-2 --threads 6 --repeat 2 \
  --text 'I have heard: Hello from our local Nest Mini.' \
  --output data/benchmarks/kitten-tts-2/repeat.wav
```

To enable the prepared backend, set `TTS_BACKEND=kitten` in `config.env` and
run `./run.sh restart`. To revert, set `TTS_BACKEND=pocket` and restart again.
The setup is for the host service; the legacy container build does not install
the optional Kitten environment.

Upstream references: [CPU runtime](https://github.com/KittenML/kitten-tts-2-cpp),
[model](https://huggingface.co/KittenML/kitten-tts-2),
[decoder choices](https://github.com/KittenML/KittenTTS/blob/main/docs/decoders.md).
Powered by KittenML. The model weights use the
[Stellon Labs Community License](https://huggingface.co/KittenML/kitten-tts-2/blob/main/LICENSE.md),
separate from the application and runtime code licenses.
