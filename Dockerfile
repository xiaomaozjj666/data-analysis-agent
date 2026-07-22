# ── Build frontend ──────────────────────────────────────────────
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Production image ────────────────────────────────────────────
FROM python:3.12-slim
LABEL app="data-analysis-agent"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir . && pip install uvicorn

# Copy app source
COPY src/ ./src/
COPY README.md ./

# Copy built frontend from builder stage
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/

EXPOSE 8000
ENV DATA_AGENT_STATIC_DIR=/app/frontend/dist
ENV DATA_AGENT_DATA_DIR=/app/data

CMD ["uvicorn", "data_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
