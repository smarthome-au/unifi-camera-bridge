FROM bluenviron/mediamtx:1.21.1@sha256:5ce2a948eb68df06ce2e13870db8df8e30d516ac4dc40e04bfe8aee3bdf7be40 AS mediamtx
FROM python:3.13-slim@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /run/bridge && chown 65532:65532 /data /run/bridge
COPY --from=mediamtx /mediamtx /mediamtx
ARG VERSION=1.6.5
ARG SOURCE_URL
ARG REVISION
LABEL org.opencontainers.image.title="Unifi Camera Bridge" \
      org.opencontainers.image.description="ONVIF and RTSP camera bridge for UniFi Protect" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.source=$SOURCE_URL \
      org.opencontainers.image.revision=$REVISION
WORKDIR /app
COPY *.py ./
COPY static ./static
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
VOLUME /data
EXPOSE 8080/tcp 8100-8115/tcp 8600-8615/tcp
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('ADMIN_PORT','8080')+'/healthz',timeout=3).read()"
CMD ["python", "manager.py"]
