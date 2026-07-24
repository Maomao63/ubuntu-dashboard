FROM python:3.13-alpine

ARG APP_VERSION=1.0.0
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST_ROOT=/host

WORKDIR /app
COPY server.py /app/server.py
COPY web /app/web

RUN addgroup -S dashboard && adduser -S -G dashboard dashboard

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/api/health >/dev/null || exit 1

CMD ["python3", "/app/server.py"]
