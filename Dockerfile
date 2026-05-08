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
FROM python:3.11-slim

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

ENV SYNAPSE_DATA_DIR=/data
ENV PYTHONPATH=/app/backend
ENV NODE_ENV=production
ENV BACKEND_URL=http://localhost:8765
ENV PORT=3000

EXPOSE 3000 8765

CMD ["/usr/bin/supervisord", "-n"]
