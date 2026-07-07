FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY modulos /app/modulos
COPY templates /app/templates
COPY static /app/static

RUN chmod -R a+rX /app

CMD ["sh", "-c", "waitress-serve --host=0.0.0.0 --port=${PORT:-9003} --call app:create_app"]
