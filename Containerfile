# syntax=docker/dockerfile:1.7

FROM docker.io/library/python:3.12-slim-bookworm AS whisper-build

ARG WHISPER_COMMIT=231638a0717b7822784297e5980965ffcec05810

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates cmake g++ git make \
 && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/ggml-org/whisper.cpp /src \
 && git -C /src checkout "$WHISPER_COMMIT"

RUN cmake -S /src -B /src/build -G "Unix Makefiles" \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
      -DWHISPER_BUILD_TESTS=OFF \
      -DWHISPER_BUILD_EXAMPLES=ON \
      -DGGML_NATIVE=OFF \
      -DGGML_AVX2=ON \
      -DGGML_FMA=ON \
      -DGGML_F16C=ON \
 && cmake --build /src/build --target whisper-cli -j "$(nproc)"


FROM docker.io/library/python:3.12-slim-bookworm

ARG POCKET_TTS_VERSION=4.0.1
ARG POCKET_TTS_TARBALL=pocket-tts-3.3.0-x86_64-unknown-linux-gnu.tar.gz
ARG POCKET_TTS_SHA256=9abff0aa138a804e44052952d8c3bdaae247fb267ca8f4b83fe178897d1a0559
ARG FUTO_STT_URL=https://keyboard.futo.org/voice-input-multilingual-244.bin
ARG FUTO_STT_SHA256=15ef255465a6dc582ecf1ec651a4618c7ee2c18c05570bbe46493d248d465ac4
ARG SHTORM_BASE=https://huggingface.co/ArtShtorm/Shtorm-PocketTTS-RU/resolve/main
ARG SHTORM_MODEL_SHA256=de8ad576d3d549372a34b40df03d6e6b8f668e857dac32f61934e9c5c1da8d52
ARG SHTORM_TOKENIZER_SHA256=95d0b8e61951408c7744e09fb5b87ff8e318612b0c8168cc593ea0a812f34f81
ARG ALBA_SHA256=46264e83cb99115c3d210260e029117566d9c64f20266d10daa78107759ede3e

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app \
    HF_HOME=/app/.cache/huggingface \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=whisper-build /src/build/bin/whisper-cli /usr/local/bin/whisper-cli

RUN mkdir -p /app/models/stt /app/models/ru /app/models/voices /app/.cache \
 && curl -fsSL "https://github.com/kyutai-labs/pocket-tts/releases/download/v${POCKET_TTS_VERSION}/${POCKET_TTS_TARBALL}" -o /tmp/pocket-tts.tar.gz \
 && echo "${POCKET_TTS_SHA256}  /tmp/pocket-tts.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/pocket-tts.tar.gz -C /tmp \
 && install -m 0755 "/tmp/pocket-tts-3.3.0-x86_64-unknown-linux-gnu/pocket-tts" /usr/local/bin/pocket-tts \
 && rm -rf /tmp/pocket-tts*

RUN curl -fsSL "$FUTO_STT_URL" -o /app/models/stt/futo.bin \
 && echo "${FUTO_STT_SHA256}  /app/models/stt/futo.bin" | sha256sum -c -

RUN curl -fsSL "$SHTORM_BASE/model_fast.safetensors" -o /app/models/ru/model_fast.safetensors \
 && echo "${SHTORM_MODEL_SHA256}  /app/models/ru/model_fast.safetensors" | sha256sum -c - \
 && curl -fsSL "$SHTORM_BASE/tokenizer.model" -o /app/models/ru/tokenizer.model \
 && echo "${SHTORM_TOKENIZER_SHA256}  /app/models/ru/tokenizer.model" | sha256sum -c -

RUN curl -fsSL "https://huggingface.co/kyutai/tts-voices/resolve/main/alba-mackenna/casual.wav" -o /tmp/alba.wav \
 && echo "${ALBA_SHA256}  /tmp/alba.wav" | sha256sum -c - \
 && ffmpeg -v error -y -i /tmp/alba.wav -t 5 -ar 24000 -ac 1 /app/models/voices/reference.wav \
 && rm -f /tmp/alba.wav

COPY models/ru-config.yaml /app/models/ru/config.yaml

RUN pocket-tts export-voice --config /app/models/ru/config.yaml /app/models/voices/reference.wav /app/models/voices/ru.safetensors -q

# Populate the English model cache during the build so the runtime does not
# depend on Hugging Face availability on first use.
RUN mkdir -p /app/.cache/huggingface \
 && pocket-tts generate --language english_7m_2026-10 --text "Ready." --output-path /tmp/ready.wav -q \
 && rm -f /tmp/ready.wav

COPY app /app/app
COPY nest_relay.py /app/nest_relay.py
COPY start-proxy.sh /app/start-proxy.sh
RUN chmod 0755 /app/start-proxy.sh \
 && python -m compileall -q /app/app

RUN mkdir -p /data/streams /data/tts /data/logs

EXPOSE 8099
CMD ["python", "-m", "app.server"]
