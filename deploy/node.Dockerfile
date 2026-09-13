ARG NODE_IMAGE=ghcr.io/morpheusais/morpheus-lumerin-node:v7.11.0@sha256:3b2b1dea272124ce3c71ab35132f5f8a6dad54bb1e59614a50401a76c062a2b1
FROM ${NODE_IMAGE} AS upstream
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NODE_DATA_DIR=/node-data
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home gateway
COPY --from=upstream /usr/bin/proxy-router /usr/local/bin/proxy-router
COPY gateway/ ./gateway/
COPY node_helper/ ./node_helper/
COPY deploy/MORPHEUS-NODE-LICENSE /usr/share/licenses/morpheus-node/LICENSE
RUN mkdir -p /node-data /control && chown -R 10001:10001 /node-data /control
USER 10001:10001
CMD ["python", "-m", "node_helper.run"]
