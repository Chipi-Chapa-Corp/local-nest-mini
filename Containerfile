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

ARG FUTO_STT_URL=https://keyboard.futo.org/voice-input-multilingual-244.bin
ARG FUTO_STT_SHA256=15ef255465a6dc582ecf1ec651a4618c7ee2c18c05570bbe46493d248d465ac4
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app \
    HF_HOME=/app/.cache/huggingface \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libgomp1 git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=whisper-build /src/build/bin/whisper-cli /usr/local/bin/whisper-cli

RUN mkdir -p /app/models/stt /app/.cache

RUN curl -fsSL "$FUTO_STT_URL" -o /app/models/stt/futo.bin \
 && echo "${FUTO_STT_SHA256}  /app/models/stt/futo.bin" | sha256sum -c -

COPY app /app/app
RUN python -m venv /opt/omnivoice \
 && /opt/omnivoice/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      'torch==2.11.0+cpu' 'torchaudio==2.11.0+cpu' \
 && /opt/omnivoice/bin/pip install --no-cache-dir 'transformers==5.20.2' accelerate numpy soundfile librosa pydub \
 && git init /opt/omnivoice-src \
 && git -C /opt/omnivoice-src remote add origin https://github.com/k2-fsa/OmniVoice.git \
 && git -C /opt/omnivoice-src fetch --depth 1 origin 08be0b4ccbac3e13e374e86fbfead4b4cac343e2 \
 && git -C /opt/omnivoice-src checkout --detach FETCH_HEAD \
 && /opt/omnivoice/bin/python -c 'import site; from pathlib import Path; (Path(site.getsitepackages()[0]) / "omnivoice-source.pth").write_text("/opt/omnivoice-src\n")'
COPY nest_relay.py /app/nest_relay.py
COPY start-proxy.sh /app/start-proxy.sh
RUN chmod 0755 /app/start-proxy.sh \
 && python -m compileall -q /app/app

RUN mkdir -p /data/streams /data/tts /data/logs

EXPOSE 8099
CMD ["python", "-m", "app.server"]
