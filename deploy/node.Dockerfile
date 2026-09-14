FROM python:3.13-slim-bookworm AS source
ADD --checksum=sha256:66f25ab5848dfa26014b866a1bd91b944a0a2897238150e81e210d613852b8e4 https://codeload.github.com/MorpheusAIs/Morpheus-Lumerin-Node/tar.gz/99a8d86af1f9453d59797f9a208aa729ff3eb00d /source.tar.gz
RUN mkdir /source && tar -xzf /source.tar.gz --strip-components=1 -C /source
COPY deploy/patch_node.py /patch_node.py
COPY deploy/consumer_node.py /consumer_node.py
COPY deploy/native /native
RUN python /patch_node.py /source/proxy-router
RUN python /consumer_node.py /source/proxy-router
RUN cp /native/go.mod /native/go.sum /source/proxy-router/

FROM golang:1.26.8-bookworm AS builder
WORKDIR /source
COPY --from=source /source/proxy-router/go.mod /source/proxy-router/go.sum ./
RUN go mod download
COPY --from=source /source/proxy-router ./
RUN gofmt -w internal/lib/gateway_*.go internal/blockchainapi/controller.go internal/blockchainapi/service.go internal/blockchainapi/structs/req.go internal/system/structs.go internal/system/controller.go internal/repositories/registries/session_router.go internal/repositories/wallet/hdwallet.go internal/proxyapi/controller_http.go internal/proxyapi/ipfs_manager.go internal/proxyapi/requests.go
RUN go test ./internal/lib -run '^TestGateway' -count=1 && go test ./internal/repositories/wallet ./internal/proxyapi -count=1
RUN go list -mod=readonly -tags docker -deps ./cmd > /tmp/runtime-packages && ! grep -E '^(golang.org/x/crypto/openpgp|github.com/ipfs/(kubo|boxo)|github.com/libp2p|github.com/quic-go|github.com/docker/docker)(/|$)' /tmp/runtime-packages
RUN CGO_ENABLED=0 GOFLAGS=-mod=readonly TAG_NAME=v7.11.0 COMMIT=99a8d86af1f9453d59797f9a208aa729ff3eb00d-gateway2 ./build.sh

FROM builder AS security
RUN GOMAXPROCS=2 GOMEMLIMIT=2GiB go run golang.org/x/vuln/cmd/govulncheck@v1.8.0 -tags docker ./cmd

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NODE_DATA_DIR=/node-data APP_ROLE=node
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home gateway
COPY --from=builder /source/proxy-router /usr/local/bin/proxy-router
COPY gateway/ ./gateway/
COPY node_helper/ ./node_helper/
COPY scripts/container_entrypoint.py /app/container_entrypoint.py
COPY deploy/MORPHEUS-NODE-LICENSE /usr/share/licenses/morpheus-node/LICENSE
RUN mkdir -p /node-data /control && chown -R 10001:10001 /node-data /control
USER 10001:10001
EXPOSE 8082 8083
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "node_helper.run"]
