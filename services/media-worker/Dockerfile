FROM rust:1.96-slim AS vobsubocr-builder

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
        --root /opt/vobsubocr \
    && rm -rf /var/lib/apt/lists/*


FROM python:3.12-slim

ARG SECONV_VERSION=v5.0.0-rc3
ARG SECONV_URL=https://github.com/SubtitleEdit/subtitleedit/releases/download/${SECONV_VERSION}/SeConv-Linux-x64.tar.gz
ARG SUBTITLEEDIT_URL=https://github.com/SubtitleEdit/subtitleedit/releases/download/${SECONV_VERSION}/SubtitleEdit-Linux-x64.tar.gz

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
        p7zip-full \
        unrar-free \
    && curl -fL "$SECONV_URL" -o /tmp/seconv.tar.gz \
    && tar -xzf /tmp/seconv.tar.gz -C /usr/local/bin ./seconv \
    && chmod +x /usr/local/bin/seconv \
    && curl -fL "$SUBTITLEEDIT_URL" -o /tmp/subtitleedit.tar.gz \
    && tar -xzf /tmp/subtitleedit.tar.gz -C /usr/local/lib ./libSkiaSharp.so ./libHarfBuzzSharp.so ./libonigwrap.so \
    && cp /usr/local/lib/libSkiaSharp.so /usr/local/bin/libSkiaSharp.so \
    && cp /usr/local/lib/libHarfBuzzSharp.so /usr/local/bin/libHarfBuzzSharp.so \
    && cp /usr/local/lib/libonigwrap.so /usr/local/bin/libonigwrap.so \
    && ldconfig \
    && rm -f /tmp/seconv.tar.gz /tmp/subtitleedit.tar.gz \
    && rm -rf /var/lib/apt/lists/*

COPY --from=vobsubocr-builder /opt/vobsubocr/bin/vobsubocr /usr/local/bin/vobsubocr

WORKDIR /opt/media-worker
COPY media_worker ./media_worker

RUN chmod -R a+rX /opt/media-worker

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "media_worker.server"]
