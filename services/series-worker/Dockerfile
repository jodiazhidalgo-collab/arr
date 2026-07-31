# El Cargo.lock del commit fijado usa time 0.3.21, incompatible con Rust >=1.80.
FROM rust:1.79-slim-bookworm AS vobsubocr-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        clang \
        libclang-dev \
        libtesseract-dev \
        libleptonica-dev \
        pkg-config \
    && cargo install \
        --git https://github.com/elizagamedev/vobsubocr \
        --rev 4f6f6919 \
        --locked \
        --root /opt/vobsubocr \
    && rm -rf /var/lib/apt/lists/*


FROM python:3.12-slim-bookworm

ARG SECONV_VERSION=v5.0.0-rc3
ARG SECONV_URL=https://github.com/SubtitleEdit/subtitleedit/releases/download/${SECONV_VERSION}/SeConv-Linux-x64.tar.gz
ARG SUBTITLEEDIT_URL=https://github.com/SubtitleEdit/subtitleedit/releases/download/${SECONV_VERSION}/SubtitleEdit-Linux-x64.tar.gz
ARG SECONV_SHA256=865e0c5d7dd851bad67b0f1f1be74cbc44529b86261e6d8ad1c6b7222554598a
ARG SUBTITLEEDIT_SHA256=a53956d088052c4b128f3246d8f54f66f06bea14b35111671dc04d822ef84e73

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        ca-certificates \
        curl \
        tesseract-ocr \
        tesseract-ocr-spa \
        fontconfig \
        libfontconfig1 \
        libfreetype6 \
    && curl -fL "$SECONV_URL" -o /tmp/seconv.tar.gz \
    && echo "$SECONV_SHA256  /tmp/seconv.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/seconv.tar.gz -C /usr/local/bin ./seconv \
    && chmod +x /usr/local/bin/seconv \
    && curl -fL "$SUBTITLEEDIT_URL" -o /tmp/subtitleedit.tar.gz \
    && echo "$SUBTITLEEDIT_SHA256  /tmp/subtitleedit.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/subtitleedit.tar.gz -C /usr/local/lib ./libSkiaSharp.so ./libHarfBuzzSharp.so ./libonigwrap.so \
    && cp /usr/local/lib/libSkiaSharp.so /usr/local/bin/libSkiaSharp.so \
    && cp /usr/local/lib/libHarfBuzzSharp.so /usr/local/bin/libHarfBuzzSharp.so \
    && cp /usr/local/lib/libonigwrap.so /usr/local/bin/libonigwrap.so \
    && ldconfig \
    && rm -f /tmp/seconv.tar.gz /tmp/subtitleedit.tar.gz \
    && rm -rf /var/lib/apt/lists/*

COPY --from=vobsubocr-builder /opt/vobsubocr/bin/vobsubocr /usr/local/bin/vobsubocr

WORKDIR /opt/series-worker
COPY series_worker ./series_worker

RUN chmod -R a+rX /opt/series-worker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERIES_WORKER_PORT=8791 \
    SERIES_WORKER_RULES_PATH=/config/series-rules/reglas_series.json \
    SERIES_WORKER_SEED_RULES_PATH=/seed/reglas_motor.json \
    SERIES_WORKER_REPORT_ROOT=/config/series-worker \
    SERIES_HEAVY_LOCK_PATH=/config/worker-locks/media-heavy.lock \
    SERIES_HEAVY_LOCK_TIMEOUT_SEC=3600

EXPOSE 8791

ENTRYPOINT ["python", "-m", "series_worker.server"]
