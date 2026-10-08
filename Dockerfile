FROM denoland/deno:bin-2.3.0 AS deno

FROM debian:bookworm-slim AS whisper-build
ARG WHISPER_VERSION=v1.8.7
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "$WHISPER_VERSION" https://github.com/ggml-org/whisper.cpp.git /whisper
RUN cmake -S /whisper -B /whisper/build -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_BLAS=OFF \
    -DWHISPER_BUILD_TESTS=OFF && cmake --build /whisper/build --target whisper-cli whisper-quantize -j 1

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
    DENO_DIR=/tmp/deno
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core fonts-lato fontconfig ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=whisper-build /whisper/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-build /whisper/build/bin/whisper-quantize /usr/local/bin/whisper-quantize
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Exported lock ensures local and container dependency parity.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /app/data /app/models && chown -R app:app /app
USER app
ENTRYPOINT ["rolki"]
CMD ["--help"]

FROM runtime AS test
USER root
RUN pip install --no-cache-dir 'pytest>=8.4,<10' 'pytest-asyncio>=1.1,<2' \
    && apt-get update && apt-get install -y --no-install-recommends espeak-ng time \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
COPY scripts ./scripts
COPY config.yaml ./config.yaml
USER app
ENTRYPOINT ["python"]
CMD ["-m", "pytest", "-q"]
