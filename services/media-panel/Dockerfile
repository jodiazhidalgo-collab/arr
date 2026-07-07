FROM python:3.12-alpine

RUN apk add --no-cache curl

WORKDIR /opt/media-panel
COPY media_panel ./media_panel

RUN chmod -R a+rX /opt/media-panel

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "media_panel.server"]
