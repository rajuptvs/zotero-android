# The standalone Zotero AI MCP server. Build from the repository root (it needs ai/spec):
#   docker build -f mcp-server/Dockerfile -t zotero-ai-mcp .
# or use compose.yaml. State and the PDF cache live in the /data volume; the Zotero API key (and a
# WebDAV password) come in as secret files, never as environment variables or arguments.
FROM python:3.12-slim

RUN pip install --no-cache-dir "pypdfium2==4.30.0" "pillow==11.3.0"
# Semantic search (only used with --semantic; the model itself downloads into /data at runtime).
RUN pip install --no-cache-dir "onnxruntime==1.31.1" "tokenizers==0.23.3" "numpy==2.5.4"

# The uid/gid that may read your secret files (compose bind-mounts them with their host owner).
ARG UID=1000
ARG GID=1000
RUN groupadd --gid "$GID" zotero \
 && useradd --uid "$UID" --gid zotero --no-create-home --home-dir /data --shell /usr/sbin/nologin zotero \
 && mkdir -p /data && chown zotero:zotero /data && chmod 700 /data

COPY ai/spec /app/ai/spec
COPY mcp-server/zotero_ai_mcp /app/mcp-server/zotero_ai_mcp
WORKDIR /app/mcp-server
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ZOTERO_AI_MCP_DATA=/data

USER zotero
VOLUME /data
EXPOSE 8770
# 0.0.0.0 inside the container only; the Host check still accepts just 127.0.0.1/localhost:8770,
# --public-url and --allowed-host, so publish the port as 127.0.0.1:8770:8770.
ENTRYPOINT ["python", "-m", "zotero_ai_mcp"]
CMD ["serve", "--host", "0.0.0.0", "--api-key-file", "/run/secrets/zotero_api_key"]
