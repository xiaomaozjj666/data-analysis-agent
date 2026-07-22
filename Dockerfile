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

# 非 root 运行：降低容器逃逸时的权限面，符合生产镜像安全基线
RUN useradd --create-home --uid 1000 --shell /sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENV DATA_AGENT_STATIC_DIR=/app/frontend/dist
ENV DATA_AGENT_DATA_DIR=/app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "data_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
