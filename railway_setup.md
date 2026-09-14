# Deploy Morpheus Consumer Gateway on Railway

Run two services in one Railway project: the gateway provides the web dashboard and application API; the node handles the consumer wallet, provider connections and on-chain sessions. The gateway can change rating configuration, restart the node and read recovery journals over an authenticated private connection.

**Image publication status:** Both images below are published on Docker Hub. Anonymous registry access and both AMD64/ARM64 manifests were verified; Railway does not need Docker Hub credentials to use this release.

## 1. Images and prerequisites

Use these matching, versioned images:

| Railway service name | Docker image | Volume | Public domain |
| --- | --- | --- | --- |
| `node` | `bowtiedbluefin/morpheus-consumer-node:railway-20260914.1` | `/node-data` | None |
| `gateway` | `bowtiedbluefin/morpheus-consumer-gateway:railway-20260914.1` | `/data` | Your dashboard/API domain |

Both images are built for `linux/amd64` and `linux/arm64`. Railway's documented image workflow targets AMD64; Apple Silicon's default ARM64 build alone is insufficient. [Railway image deployment guide](https://docs.railway.com/guides/private-container-registry)

Published image index digests (use `IMAGE@sha256:...` instead of the tag to pin the exact release):

| Image | Digest |
| --- | --- |
| `bowtiedbluefin/morpheus-consumer-gateway` | `sha256:eaa1e95d9c1f46d15f563acf494b8400045109aa90a558a76ba3097a2f1240f2` |
| `bowtiedbluefin/morpheus-consumer-node` | `sha256:fcf032f026a3566e4874990c69c7459d12bf4e59f275fa7021f4c6b48fcd7df6` |

You can inspect the published release before recording:

```sh
docker buildx imagetools inspect bowtiedbluefin/morpheus-consumer-node:railway-20260914.1
docker buildx imagetools inspect bowtiedbluefin/morpheus-consumer-gateway:railway-20260914.1
```

You need:

- A Railway project that can run two persistent services with one volume each.
- A dedicated consumer wallet containing MOR and ETH on **Base mainnet**.
- A working Base mainnet RPC URL, including its provider key if required.
- Three independent random secrets: the dashboard admin token, node password and management-helper token.

Create each secret with a password manager, or run this command three times privately:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Keep the wallet private key and secrets off the video. Show placeholder values while explaining them. Credentials belong in Railway Variables; they are not embedded in the Docker images.

Use one gateway/node pair per wallet. Before moving an existing wallet here, pause the old gateway, stop keep-available policies, finish active requests, close its sessions, and stop both old services. Preserve both old volumes and secrets for recovery. Never run two independent gateway installations against the same wallet. Importing a wallet key alone does not transfer its existing gateway settings or uncertain-operation history.

## 2. Create the two Railway services

Create a project and add two services from **Docker Image**. Use the image names above and name the services exactly `node` and `gateway`; the reference variables below depend on those names. Keep both services in the same project environment and region.

Attach the volumes before the first successful startup. Railway mounts a service's volume at the configured path at runtime. Keep one replica per service, disable Serverless/sleep, and keep the default image start command. [Railway volumes](https://docs.railway.com/volumes), [volume limits](https://docs.railway.com/volumes/reference)

These are public prebuilt images, so no source build, pre-deploy command or Docker Hub login is needed in Railway. If you later use a private image fork, configure registry credentials in each service's Source settings. Those credentials are separate from all application secrets below. Railway currently requires Pro for directly deploying private registry images. [Private registries](https://docs.railway.com/builds/private-registries)

### GitHub build alternative

As an alternative to the prebuilt images, connect both services to `bowtiedbluefin/morpheus-consumer-gateway`, selecting branch `fix/recovery-and-customer-reliability` until this change is merged. Keep the repository root as the Root Directory. For the gateway use `Dockerfile`; for the node set `RAILWAY_DOCKERFILE_PATH=deploy/node.Dockerfile`. The same volumes, variables and health checks below apply. This requires Railway access to the private repository and builds the node in Railway. [Dockerfiles](https://docs.railway.com/builds/dockerfiles)

## 3. Configure the node

Open **node → Variables → Raw Editor**. Paste this, replacing the four credential/RPC placeholders:

```dotenv
ENVIRONMENT=production
ETH_NODE_ADDRESS=https://YOUR-BASE-RPC-ENDPOINT
ETH_NODE_CHAIN_ID=8453
WALLET_PRIVATE_KEY=YOUR-DEDICATED-WALLET-PRIVATE-KEY
NODE_PASSWORD=YOUR-RANDOM-NODE-PASSWORD
HELPER_TOKEN=YOUR-SEPARATE-RANDOM-HELPER-TOKEN
HELPER_TRANSPORT=http
HELPER_HOST=::
HELPER_PORT=8083
PORT=8083
WEB_ADDRESS=:8082
PROXY_ADDRESS=127.0.0.1:3333
NODE_DATA_DIR=/node-data
IPFS_DISABLED=true
LOG_LEVEL_APP=warn
LOG_LEVEL_TCP=warn
LOG_LEVEL_ETH_RPC=warn
RAILWAY_RUN_UID=0
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=180
RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0
RAILWAY_HEALTHCHECK_TIMEOUT_SEC=300
```

`WEB_ADDRESS=:8082` is deliberate: the pinned native validator rejects a literal bracketed IPv6 host. An empty host binds all interfaces; both IPv4 and IPv6 were verified against the built image.

Set the service healthcheck path to **`/healthz`**. `PORT=8083` directs Railway's health check to the helper; the node API remains on 8082. This check exposes only whether the native child is running, not credentials, configuration or journals. The gateway's readiness check verifies the fuller node/control connection. [Railway health checks](https://docs.railway.com/deployments/healthchecks)

**Do not generate a public HTTP domain or TCP proxy for `node`.** Neither port 8082 nor 8083 needs public exposure. The consumer opens outbound connections to providers; this setup is not a provider node accepting public traffic.

The helper requires a 32–256 character URL-safe token and rejects HTTP startup without one. Its status, restart and journal endpoints require `Authorization: Bearer <HELPER_TOKEN>`. The gateway adds that header automatically.

`RAILWAY_RUN_UID=0` is for the container entrypoint to initialize the root-owned volume. The entrypoint fixes ownership and then drops to UID/GID **10001 before starting the helper and native node**. It does not leave the application running as root. Do not replace the entrypoint with a custom start command. [Railway volume permissions](https://docs.railway.com/volumes)

Deploy the node first. Initial RPC connection and wallet setup can take time. The expected outcome is a running node process and a successful helper health check.

## 4. Configure the gateway

Open **gateway → Variables → Raw Editor** and paste:

```dotenv
ADMIN_TOKEN=YOUR-SEPARATE-RANDOM-DASHBOARD-SECRET
NODE_URL=http://${{node.RAILWAY_PRIVATE_DOMAIN}}:8082
NODE_USERNAME=admin
NODE_PASSWORD=${{node.NODE_PASSWORD}}
HELPER_URL=http://${{node.RAILWAY_PRIVATE_DOMAIN}}:8083
HELPER_TOKEN=${{node.HELPER_TOKEN}}
RPC_URL=${{node.ETH_NODE_ADDRESS}}
DATA_DIR=/data
HOST=::
PORT=8000
PUBLIC_ORIGIN=https://YOUR-GATEWAY-DOMAIN
COOKIE_SECURE=true
RAILWAY_RUN_UID=0
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=180
RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0
RAILWAY_HEALTHCHECK_TIMEOUT_SEC=300
```

Enter `ADMIN_TOKEN` only on the gateway. The wallet private key belongs only on the node. The gateway references the existing node password, helper token and RPC URL instead of making copies that can drift. Railway resolves this `${{service.VARIABLE}}` syntax; do not substitute the literal strings into a plain Docker env file. [Railway variables](https://docs.railway.com/variables)

Leave `HELPER_SOCKET` unset on Railway. `HELPER_URL` selects the network transport; setting both deliberately fails startup. Also leave `ADMIN_TOKEN_FILE`, `NODE_PASSWORD_FILE`, `HELPER_TOKEN_FILE`, `WALLET_PRIVATE_KEY_FILE` and `RPC_URL_FILE` unset when supplying values through Variables. File variables take precedence and would point at nonexistent Docker Compose secret mounts.

Open **gateway → Settings → Networking**, generate a public domain with target port **8000**, and replace `PUBLIC_ORIGIN` with that exact HTTPS origin, without a trailing slash or `/v1`. Deploy the staged changes. A custom domain works too; update `PUBLIC_ORIGIN` to match the URL used in the browser.

Set the gateway healthcheck path to **`/readyz`**. It requires the node to be ready and, in network-helper mode, an authenticated ready helper. A wrong helper token must not produce a green deployment. `/healthz` is only the gateway's basic process check.

The private service addresses use HTTP inside Railway's encrypted private network. Both listeners support IPv4 and IPv6, including older Railway environments with IPv6-only private DNS. [Private networking](https://docs.railway.com/networking/private-networking/how-it-works)

The two 180-second draining settings give shutdown time before forced termination. Volume-backed service redeploys still have downtime; overlapping two wallet owners is not a supported availability strategy. [Deployment teardown](https://docs.railway.com/deployments/deployment-teardown)

## 5. Verify the dashboard and configure a model

Open `https://YOUR-GATEWAY-DOMAIN` and enter `ADMIN_TOKEN` in **Administrator secret**.

Check **Overview**:

- The displayed wallet is the intended dedicated consumer wallet.
- Network is Base mainnet, chain ID 8453; MOR and ETH balances match that wallet.
- The node and helper are connected, and no unresolved opening/closing operation is reported.
- The displayed API base URL uses your HTTPS domain and ends in `/v1`.

In **Models**, browse the catalog, select a model and give it a convenient alias. For the previously tested DeepSeek model:

```text
Alias: deepseek-v4-flash
Model ID: 0xc2c4b037ff12e0aa81178deac52aeed902b36189b9e6feae22b72324c9221130
```

Catalog presence and provider capacity are live conditions. Verify availability in your installation; this identifier does not promise that a provider is always available.

Set the session duration to **30 minutes** for the walkthrough, choose **until expiry** retention and save. This reuses an opened session until it expires; it does not open one until needed. **Keep available** maintains replacement sessions while its configured deadline, policy, capacity and wallet limits permit. For a short funded acceptance check, five minutes is supported.

In **Wallet & recovery**, set stake limits and an ETH reserve appropriate for this wallet before opening anything. Leave automatic recovery and withdrawal enabled. Keep automatic cleanup of untracked *live* sessions disabled unless you intentionally want the gateway to close them.

In **Providers & rating**, configure allowlists/blocklists and the rating weights. Save policy changes. **Apply rating and restart** writes the node's rating file and restarts its native process, while the gateway UI remains available. A provider added to the blocklist is also blocked by the gateway for reuse of an existing session.

## 6. Create an application key and send a prompt

In **API keys**, create a key with the desired model scope and concurrency limit. Copy it immediately into your client's secret storage. This is the `mg_...` key used by applications, not the dashboard secret, node password, wallet key or helper token.

With `MORPHEUS_API_KEY` already loaded privately into your local shell:

```sh
export MORPHEUS_BASE_URL=https://YOUR-GATEWAY-DOMAIN/v1

curl --fail-with-body "$MORPHEUS_BASE_URL/models" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY"

curl --fail-with-body --max-time 360 "$MORPHEUS_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with a short greeting."}],"max_tokens":2048}'
```

The first prompt can take longer because the node negotiates a provider and opens an on-chain session. Session opening escrows MOR and uses ETH for gas. Keep **Sessions** open in another tab so the viewer can see the session appear.

Test streaming through Railway's actual public URL:

```sh
curl -N --fail-with-body --max-time 360 "$MORPHEUS_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"Explain consumer nodes in two sentences."}],"max_tokens":2048}'
```

Expect incremental `data:` events ending in `data: [DONE]`. If a stream has already begun, a subsequent error is carried in the stream and may still have HTTP status 200. Inspect the events, not just curl's exit status. Do not automatically replay a timed-out prompt: inspect Sessions first because its opening may already have mined.

## 7. End-to-end acceptance before recording

| Action | Expected result |
| --- | --- |
| Wrong application key | 401; no session opened. |
| Wrong model alias | 404; no new session or wallet transaction. |
| First valid prompt | One eligible provider session opens; reply arrives. |
| Second prompt with spare time on that session | Existing session is reused, respecting the configured pool limits. |
| Two concurrent prompts | Replies or a bounded, explained queue/capacity error; no service hang. To require concurrent execution, allow two sessions and sufficient key concurrency/collateral. |
| Streaming via the public domain | Incremental events and a final `[DONE]`. |
| Apply rating and restart from the UI | Operation becomes succeeded; node restarts and the saved rating becomes effective; dashboard remains available. |
| Prompt after restart | Inference recovers and uses or replaces eligible sessions according to policy. |
| Redeploy only the gateway | Model settings, keys and session records persist from `/data`; it reconnects to the same node. |
| Redeploy only the node after draining requests | Rating, native storage and operation journals persist in `/node-data`; gateway eventually returns to ready. |
| Temporarily override gateway `HELPER_TOKEN` with a different generated value | `/readyz` fails and management actions cannot execute. Restore `${{node.HELPER_TOKEN}}` and redeploy to recover. |
| Close a test session | It becomes closed after chain confirmation. Repeating close does not open another session. |
| Recovery after withdrawal eligibility | Eligible MOR is withdrawn if policy and gas reserve permit; day-locked MOR waits for the contract's release window. |

Before testing redeploys, disable keep-available replacement, pause admissions and wait for active requests to finish. Restore normal settings afterward. Never delete persistent volumes or unresolved operations to clear an error.

## 8. Video sequence

1. Show the two image names and separate Railway service cards.
2. Show the volume mount paths and explain the four credential roles with placeholders.
3. Show the gateway's reference variables pointing at the node.
4. Generate the gateway domain, log in and verify the wallet/network.
5. Add DeepSeek, choose 30 minutes and save provider/rating settings.
6. Create an application key off-camera; show a masked configured client.
7. Send the first prompt, show the on-chain session appear, then send another prompt using it.
8. Demonstrate streaming and a node restart from the UI.
9. Close the test session and explain liquid, held and withdrawable MOR.

## 9. Troubleshooting and maintenance

| Symptom | Check |
| --- | --- |
| Image pull denied / manifest unknown | Confirm the tag was published and the namespace is correct. Configure registry credentials for private images. Docker Hub credentials are unrelated to `NODE_PASSWORD`. |
| Node reports missing wallet/password | Set node Variables with values, not `_FILE` paths copied from Compose. Check the password is at least 32 characters. |
| Startup fails with helper token error | Generate a URL-safe 32–256 character token; use the same value through the gateway's reference variable. |
| `/readyz` is 503 but `/healthz` is 200 | Check node RPC/wallet readiness, private URLs, helper token and helper readiness. Gateway's admin status shows an actionable helper authentication error. |
| Permission denied on `/data` or `/node-data` | Confirm the volume path, `RAILWAY_RUN_UID=0` and the unchanged image entrypoint. Applications should run as UID 10001 after initialization. |
| Login or Save is rejected | `PUBLIC_ORIGIN` must exactly match the browser's HTTPS origin; keep `COOKIE_SECURE=true`. Use the gateway domain, not a node domain. |
| Private connection refused | Verify same Railway environment, `:8082` on the node API, helper `::` on 8083 and the exact service name in references. |
| Healthcheck points at the wrong port | Node `PORT=8083`, gateway `PORT=8000`; health paths differ. |
| No permitted provider / insufficient collateral | Check model availability, provider restrictions, cooldowns, budget, MOR and ETH. Do not disable transaction guards. |
| MOR remains held after close | This can be the normal contract lock. Wait until eligible and confirm recovery remains enabled with enough ETH. |
| Gateway reports wallet/network changed | It is protecting the existing installation. Restore the intended node identity or perform a deliberate migration with matching data; do not erase the database as a shortcut. |

Back up both volumes and store the deployment secrets separately. For a consistent manual backup, pause admissions, stop replacement policies, finish requests and wallet operations, stop services, then snapshot both volumes. Restore the pair with the same wallet/network and only one active installation. Retain unknown-operation journals until reconciled. Upgrade to a new matching image pair deliberately, after draining; avoid automatic image updates for a wallet-owning service.

## 10. Build, publish and validation provenance

From the repository root, with Docker Hub logged into an account allowed to publish under `bowtiedbluefin`:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -f Dockerfile \
  -t bowtiedbluefin/morpheus-consumer-gateway:railway-20260914.1 \
  --push .

docker buildx build --platform linux/amd64,linux/arm64 \
  -f deploy/node.Dockerfile \
  -t bowtiedbluefin/morpheus-consumer-node:railway-20260914.1 \
  --push .
```

For subsequent releases, use a new matching immutable tag rather than overwriting the demonstrated tag. Do not pass wallet/private credentials as build arguments. The Dockerfiles copy only application/runtime assets; `.dockerignore` excludes local secrets and test data.

The node remains pinned to upstream v7.11.0 with the existing collateral/journal/recovery patches and upgraded dependencies. The [remediation report](docs/REMEDIATION-REPORT.md) explains its security disposition and broader production acceptance limits.

The published gateway includes source through commit `46c9eea`; the node image was built from `cfa23f5` (the subsequent commit only formats gateway favicon markup). [GitHub CI passed for `46c9eea`](https://github.com/bowtiedbluefin/morpheus-consumer-gateway/actions/runs/34802724846), including backend/browser checks, both Docker builds, volume ownership checks and the native source security gate. Both public registry manifests returned HTTP 200 without account credentials and include `linux/amd64` and `linux/arm64`.

**Completed local verification:** 167 backend tests passed; both architecture builds passed; both AMD64 images initialized root-owned mounts and ran application code as UID 10001. The isolated pair passed startup, wrong-model rejection without opening a session, rating application/native restart over private HTTP, and preservation of wallet identity, rating, keys and operation history after replacing both containers. Native API/helper access passed on IPv4 and IPv6; unauthenticated helper access was rejected. The existing native source security gate passed. No funded transaction was submitted by this Railway packaging smoke test.

The new automated tests exercise authenticated HTTP management over real TCP, unauthorized access, restart idempotency, journal reads, response/body limits, IPv4/IPv6 sockets and readiness with a bad helper token. A local smoke deployment using `deploy/railway-smoke.compose.yaml` reproduces separate containers, independent root-owned volumes and private HTTP without a shared control socket. This is local verification, not a claim that a Railway-hosted deployment or funded public HTTPS inference has already passed. Complete section 7 on the actual project before recording a successful end-to-end deployment.
