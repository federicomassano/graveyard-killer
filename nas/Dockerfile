# gk-fetcher: the plugin's own post.py behind a small HTTP server. Build from the repo root:
#   docker build -f nas/Dockerfile -t gk-fetcher:local .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv
ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin
RUN uv tool install yt-dlp && uv tool install gallery-dl
COPY plugin/skills/graveyard-killer/scripts/post.py plugin/skills/graveyard-killer/scripts/link.py \
     plugin/skills/graveyard-killer/scripts/nas.py nas/server.py /app/
ENV GK_WORK=/work GK_POST=/app/post.py PYTHONUNBUFFERED=1
RUN useradd -u 568 -m gk && mkdir /work && chown 568 /work
USER 568
EXPOSE 8080
CMD ["python", "/app/server.py"]
