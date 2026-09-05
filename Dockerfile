FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_PATH=/data/config.json \
    TRACE_DB_PATH=/data/auditer.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md LICENSE.MIT ./
RUN mkdir -p ./src
COPY src/sub2api_auditer ./src/sub2api_auditer

RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home --home-dir /home/auditer auditer \
    && mkdir -p /data \
    && chown -R auditer:auditer /data /home/auditer

USER auditer
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

CMD ["sub2api-auditer", "--host", "0.0.0.0", "--port", "8080"]
