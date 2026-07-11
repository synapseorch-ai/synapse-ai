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

RUN apt-get update && apt-get install -y \
    curl build-essential libpq-dev supervisor \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
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

ENV SYNAPSE_DATA_DIR=/data
ENV PYTHONPATH=/app/backend
ENV NODE_ENV=production
ENV SYNAPSE_BACKEND_PORT=8765
ENV SYNAPSE_FRONTEND_PORT=3000
# Auto-generate a shared internal token on first boot so the backend's
# InternalTokenMiddleware enforces by default. Both supervisord programs inherit
# it from the entrypoint's exported environment. Persisted under /data.
ENV SYNAPSE_AUTOGEN_TOKEN=1
ENV SYNAPSE_SECRETS_DIR=/data
ENV SYNAPSE_TOKEN_MODE=generate

EXPOSE 3000 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n"]
