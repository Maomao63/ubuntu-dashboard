FROM node:22-alpine AS terminal-assets

RUN npm install --prefix /terminal-assets \
    @xterm/xterm@5.5.0 \
    @xterm/addon-fit@0.10.0

FROM python:3.13-alpine

ARG APP_VERSION=1.11.0
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST_ROOT=/host

WORKDIR /app
COPY server.py /app/server.py
COPY web /app/web
COPY --from=terminal-assets /terminal-assets/node_modules/@xterm/xterm/lib/xterm.js /app/web/vendor/xterm.js
COPY --from=terminal-assets /terminal-assets/node_modules/@xterm/xterm/css/xterm.css /app/web/vendor/xterm.css
COPY --from=terminal-assets /terminal-assets/node_modules/@xterm/addon-fit/lib/addon-fit.js /app/web/vendor/addon-fit.js
COPY --from=terminal-assets /terminal-assets/node_modules/@xterm/xterm/LICENSE /app/web/vendor/xterm-LICENSE
COPY --from=terminal-assets /terminal-assets/node_modules/@xterm/addon-fit/LICENSE /app/web/vendor/addon-fit-LICENSE

RUN apk add --no-cache ca-certificates openssh-client sshpass smartmontools \
    && addgroup -S dashboard \
    && adduser -S -G dashboard dashboard

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/api/health >/dev/null || exit 1

CMD ["python3", "/app/server.py"]
