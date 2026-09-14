FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS gateway
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data STATIC_DIR=/app/web/dist APP_ROLE=gateway
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home gateway
COPY gateway/ ./gateway/
COPY scripts/container_entrypoint.py /app/container_entrypoint.py
COPY --from=web /web/dist ./web/dist
RUN mkdir -p /data /control && chown -R 10001:10001 /data /control
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "gateway.run"]
