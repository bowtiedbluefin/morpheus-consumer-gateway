FROM python:3.13-slim-bookworm AS source
ADD --checksum=sha256:66f25ab5848dfa26014b866a1bd91b944a0a2897238150e81e210d613852b8e4 https://codeload.github.com/MorpheusAIs/Morpheus-Lumerin-Node/tar.gz/99a8d86af1f9453d59797f9a208aa729ff3eb00d /source.tar.gz
RUN mkdir /source && tar -xzf /source.tar.gz --strip-components=1 -C /source
COPY deploy/patch_node.py /patch_node.py
COPY deploy/native /native
RUN python /patch_node.py /source/proxy-router

FROM golang:1.25-bookworm AS builder
WORKDIR /source
COPY --from=source /source/proxy-router/go.mod /source/proxy-router/go.sum ./
RUN go mod download
COPY --from=source /source/proxy-router ./
RUN gofmt -w internal/lib/gateway_progress*.go internal/blockchainapi/controller.go internal/blockchainapi/service.go internal/blockchainapi/structs/req.go internal/system/structs.go internal/system/controller.go internal/repositories/registries/session_router.go
RUN go test ./internal/lib -run '^TestGateway' -count=1
RUN CGO_ENABLED=0 TAG_NAME=v7.11.0 COMMIT=99a8d86af1f9453d59797f9a208aa729ff3eb00d-gateway1 ./build.sh

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NODE_DATA_DIR=/node-data
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home gateway
COPY --from=builder /source/proxy-router /usr/local/bin/proxy-router
COPY gateway/ ./gateway/
COPY node_helper/ ./node_helper/
COPY deploy/MORPHEUS-NODE-LICENSE /usr/share/licenses/morpheus-node/LICENSE
RUN mkdir -p /node-data /control && chown -R 10001:10001 /node-data /control
USER 10001:10001
CMD ["python", "-m", "node_helper.run"]
