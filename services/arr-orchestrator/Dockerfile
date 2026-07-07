FROM jlesage/filebot:latest

RUN apk add --no-cache python3 py3-pip

WORKDIR /opt/arr-orchestrator

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt \
    && python3 -c "from guessit import guessit; result=guessit('Treme.1x03.Right.Place.Wrong.Time.HDTV.XviD-NoTV.avi'); assert result['type'] == 'episode'"

COPY arr_orchestrator ./arr_orchestrator
COPY tests ./tests
COPY hooks ./hooks

RUN chmod -R a+rX /opt/arr-orchestrator \
    && chmod +x /opt/arr-orchestrator/hooks/qbt-complete.sh

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "arr_orchestrator.main"]
