# Stage 1: Build Next.js frontend
FROM node:20-alpine AS frontend-builder
ARG BACKEND_URL=http://localhost:8765
ENV BACKEND_URL=$BACKEND_URL
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: Combined runtime (Python backend + Node.js frontend)
FROM python:3.13-slim

# Node 20 + npm for the npx-based MCP servers, from Debian (base is Debian 13
# trixie, whose nodejs is already v20). Do NOT go back to
# `curl -fsSL …nodesource… | bash -`: the pipe discards curl's exit code, so an
# intermittent 403 fell through to Debian's `nodejs` — which ships only
# /usr/bin/node, no npm/npx. This image pre-installs no MCP servers and resolves
# every one through `npx` at runtime, so v1.8.4 shipped with all of them broken.
# npm is a separate package here; the assertion below keeps it from vanishing again.
RUN apt-get update && apt-get install -y \
    curl build-essential libpq-dev supervisor nodejs npm \
    && node --version && npx --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
# Install from the lockfile so images resolve the exact tree CI tested.
# requirements.txt is copied too, as the fallback and for provenance.
COPY backend/requirements.txt backend/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
RUN pip install playwright && playwright install chromium --with-deps

COPY backend/core ./core
COPY backend/services ./services
COPY backend/tools ./tools
COPY backend/main.py .

WORKDIR /app/frontend
COPY --from=frontend-builder /app/.next/standalone ./
COPY --from=frontend-builder /app/.next/static ./.next/static
COPY --from=frontend-builder /app/public ./public

COPY docker/supervisord.conf /etc/supervisor/conf.d/synapse.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/app/backend
ENV NODE_ENV=production
ENV SYNAPSE_BACKEND_PORT=8765
ENV SYNAPSE_FRONTEND_PORT=3000

# One volume, /data — the path every 1.x install mounted (`-v synapse-data:/data`),
# so the same `docker run` keeps its data across the upgrade. What is on it is
# this install's own: the database, the content under blobs/, and the secrets.
# Rebuildable caches and scratch stay off it.
#
# The database is deployment configuration. SQLite on the volume is the default
# so `docker run` works with nothing else running; point SYNAPSE_DB_URL at
# Postgres for anything with more than one process.
ENV SYNAPSE_BLOB_DIR=/data/blobs
ENV SYNAPSE_DB_URL=sqlite+aiosqlite:////data/synapse.db
# Rebuildable and per-replica, so it deliberately does NOT go on the volume.
ENV SYNAPSE_STATE_DIR=/var/lib/synapse/state
# Where a 1.x volume keeps its JSON: read once, on the first boot, to import it
# (backend/core/store/importer.py), then moved into /data/migrated/.
ENV SYNAPSE_DATA_DIR=/data
VOLUME /data

# Auto-generate a shared internal token on first boot so the backend's
# InternalTokenMiddleware enforces by default. Both supervisord programs inherit
# it from the entrypoint's exported environment. Persisted on the volume — where
# a 1.x install already has one, which keeps its existing sessions working.
ENV SYNAPSE_AUTOGEN_TOKEN=1
ENV SYNAPSE_SECRETS_DIR=/data
ENV SYNAPSE_TOKEN_MODE=generate

EXPOSE 3000 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n"]
