# Deploy Morpheus Consumer Gateway on Railway

Run two services in one Railway project: the gateway provides the web dashboard and application API; the node handles the consumer wallet, provider connections and on-chain sessions. The gateway can change rating configuration, restart the node and read recovery journals over an authenticated private connection.

**Image publication status:** Both images below are published on Docker Hub. Anonymous registry access and both AMD64/ARM64 manifests were verified; Railway does not need Docker Hub credentials to use this release.

**Walkthrough status, September 14, 2026:** The operator confirmed that the Railway node booted, dashboard login worked after correcting the HTTPS browser origin, and a chat-completion curl request from their computer succeeded. This guide incorporates the setup errors encountered along the way. The full restart/recovery/concurrency acceptance matrix in section 7 remains to be verified on Railway.

## 1. Images and prerequisites

Use these matching, versioned images:

| Railway service name | Docker image | Volume | Public domain |
| --- | --- | --- | --- |
| `morpheus-consumer-node` | `bowtiedbluefin/morpheus-consumer-node:railway-20260914.1` | `/node-data` | None |
| `morpheus-consumer-gateway` | `bowtiedbluefin/morpheus-consumer-gateway:railway-20260914.1` | `/data` | Your dashboard/API domain |

Both images are built for `linux/amd64` and `linux/arm64`. Railway's documented image workflow targets AMD64; Apple Silicon's default ARM64 build alone is insufficient. [Railway image deployment guide](https://docs.railway.com/guides/private-container-registry)

The consumer-node image is our modified v7.11.0 build, including native collateral safeguards, transaction journals, dependency changes and the management helper. This gateway release depends on those additions. Replacing it with the stock upstream GHCR image is not a supported image swap. The helper is what lets the UI change rating configuration and restart the native process without access to the Docker daemon.

When inspecting upstream images, distinguish an image digest (`@sha256:...`) from a similarly named tag (`:sha256-...`). In this walkthrough, upstream tag `sha256-3b2b1dea272124ce3c71ab35132f5f8a6dad54bb1e59614a50401a76c062a2b1` contained Sigstore signature metadata; the corresponding runnable image was `ghcr.io/morpheusais/morpheus-lumerin-node@sha256:3b2b1dea272124ce3c71ab35132f5f8a6dad54bb1e59614a50401a76c062a2b1`. Registry inspection confirmed that distinction. Continue using the bundled image above for this gateway release.

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

| Credential | Where to enter it | Requirement / purpose |
| --- | --- | --- |
| `ADMIN_TOKEN` | Gateway Variables; later the dashboard login field | At least 32 characters; use its own generated secret. |
| `NODE_PASSWORD` | Node Variables; referenced by gateway | At least 32 characters; native API username is `admin`. |
| `HELPER_TOKEN` | Node Variables; referenced by gateway | 32–256 characters using letters, numbers, `_` or `-`; authenticates management calls. |
| `WALLET_PRIVATE_KEY` | Node Variables only | Your dedicated consumer wallet key, not a newly generated password. |
| Application API key (`mg_...`) | Create later in the dashboard | Used by curl and application clients; separate from all deployment secrets. |

Short values such as `test1234` and `test5678` are rejected. Generate the three passwords/tokens before deploying. Rotate credentials exposed in screenshots or a recording; revoke an exposed application key and issue a new one.

Keep the wallet private key and secrets off the video. Show placeholder values while explaining them. Credentials belong in Railway Variables; they are not embedded in the Docker images.

Use one gateway/node pair per wallet. Before moving an existing wallet here, pause the old gateway, stop keep-available policies, finish active requests, close its sessions, and stop both old services. Preserve both old volumes and secrets for recovery. Never run two independent gateway installations against the same wallet. Importing a wallet key alone does not transfer its existing gateway settings or uncertain-operation history.

## 2. Create the two Railway services

Create a project and add two separate services from **Docker Image**, one for each image above. A Dockerfile builds an image; Railway runs the published image names directly. Keep both services in the same project environment and region.

This guide uses the service names **`morpheus-consumer-node`** and **`morpheus-consumer-gateway`**, matching the image names. Check the actual names on your project service cards before copying variables. Throughout the guide, “node” and “gateway” mean those two services. If you choose different service names, replace `morpheus-consumer-node` in every gateway reference with your node service's exact name. An earlier version of this guide used the shorter name `node`; those references resolve incorrectly when the actual service has a different name.

Attach the volumes before the first successful startup. Railway mounts a service's volume at the configured path at runtime. Keep one replica per service, disable Serverless/sleep, and keep the default image start command. [Railway volumes](https://docs.railway.com/volumes), [volume limits](https://docs.railway.com/volumes/reference)

These are public prebuilt images, so no source build, pre-deploy command or Docker Hub login is needed in Railway. If you later use a private image fork, configure registry credentials in each service's Source settings. Those credentials are separate from all application secrets below. Railway currently requires Pro for directly deploying private registry images. [Private registries](https://docs.railway.com/builds/private-registries)

### First deployment and getting the hostnames

Use this order during setup:

1. Create both services and attach `/node-data` to the node and `/data` to the gateway.
2. Configure the node using section 3, apply the Variables and deploy it.
3. On the **node service**, open **Settings → Networking → Private Networking**. Copy its private hostname. With the names above it will normally be `morpheus-consumer-node.railway.internal`; use the value Railway actually shows.
4. Configure the gateway's node connections using section 4 and inspect their resolved values.
5. On the **gateway service**, generate its public HTTPS domain targeting port 8000. Set `PUBLIC_ORIGIN` from that domain, apply changes and deploy the gateway.
6. Open the HTTPS dashboard, log in, configure a model and run the curl test.

Railway may attempt an initial deployment as soon as you add an image, before variables are complete. If that attempt fails for a missing secret, malformed helper URL or an unavailable node, finish the relevant configuration and redeploy. You do not need a successful application startup to read the service's networking settings, and you do not need to deliberately cause a failure. If you already started the gateway with a placeholder origin, replace it after generating the domain and redeploy before logging in. A failed setup attempt does not require deleting services or volumes.

The node's private hostname, gateway's private hostname and gateway's public domain are three different addresses. The private hostname shown on the **gateway** card must not be used for `NODE_URL` or `HELPER_URL`.

### GitHub build alternative

As an alternative to the prebuilt images, connect both services to `bowtiedbluefin/morpheus-consumer-gateway`, selecting branch `fix/recovery-and-customer-reliability` until this change is merged. Keep the repository root as the Root Directory. For the gateway use `Dockerfile`; for the node set `RAILWAY_DOCKERFILE_PATH=deploy/node.Dockerfile`. The same volumes, variables and health checks below apply. This requires Railway access to the private repository and builds the node in Railway. [Dockerfiles](https://docs.railway.com/builds/dockerfiles)

## 3. Configure the node

Open **node → Variables → Raw Editor**. Paste this, replacing the four credential/RPC placeholders:

```dotenv
ENVIRONMENT=production
ETH_NODE_ADDRESS=https://YOUR-BASE-RPC-ENDPOINT
ETH_NODE_CHAIN_ID=8453
DIAMOND_CONTRACT_ADDRESS=0x6aBE1d282f72B474E54527D93b979A4f64d3030a
MOR_TOKEN_ADDRESS=0x7431aDa8a591C955a994a21710752EF9b882b8e3
BLOCKSCOUT_API_URL=https://base.blockscout.com/api/v2
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

Railway Variables become the container's environment; you do not upload a `.env` file. The block above explicitly identifies Base mainnet, its marketplace contract, MOR token and explorer. Keep these values together with a matching Base RPC. [Official network addresses](https://nodedocs.mor.org/get-started/networks-and-tokens)

The helper supplies network defaults when the contract/explorer variables are absent. Release `railway-20260914.1` had an incorrect `/api` explorer default; explicitly setting `BLOCKSCOUT_API_URL=https://base.blockscout.com/api/v2` fixes that release without rebuilding. The native node's transaction-history client requires `/api/v2`.

The helper also generates native authentication from `NODE_PASSWORD`, writes the rating configuration, and sets cookie, authentication, storage and recovery-journal paths under `/node-data`. It disables stored/forwarded chat context because API clients supply message history. Do not copy the upstream example's `admin:admin` credentials, Docker socket settings or demo local-model configuration into this deployment. Other optional native settings retain the pinned node's defaults unless supplied as Railway Variables.

`WEB_ADDRESS=:8082` is deliberate: the pinned native validator rejects a literal bracketed IPv6 host. An empty host binds all interfaces; both IPv4 and IPv6 were verified against the built image.

Set the service healthcheck path to **`/healthz`**. `PORT=8083` directs Railway's health check to the helper; the node API remains on 8082. This check exposes only whether the native child is running, not credentials, configuration or journals. The gateway's readiness check verifies the fuller node/control connection. [Railway health checks](https://docs.railway.com/deployments/healthchecks)

**Do not generate a public HTTP domain or TCP proxy for `node`.** Neither port 8082 nor 8083 needs public exposure. The consumer opens outbound connections to providers; this setup is not a provider node accepting public traffic.

The helper requires a 32–256 character URL-safe token and rejects HTTP startup without one. Its status, restart and journal endpoints require `Authorization: Bearer <HELPER_TOKEN>`. The gateway adds that header automatically.

`RAILWAY_RUN_UID=0` is for the container entrypoint to initialize the root-owned volume. The entrypoint fixes ownership and then drops to UID/GID **10001 before starting the helper and native node**. It does not leave the application running as root. Do not replace the entrypoint with a custom start command. [Railway volume permissions](https://docs.railway.com/volumes)

Deploy the node first. Initial RPC connection and wallet setup can take time. The expected outcome is a running node process and a successful helper health check.

## 4. Configure the gateway

Open **gateway → Variables → Raw Editor** and paste the following, replacing the admin-secret and public-domain placeholders. These references assume the node service is named exactly `morpheus-consumer-node`:

```dotenv
ADMIN_TOKEN=YOUR-SEPARATE-RANDOM-DASHBOARD-SECRET
NODE_URL=http://${{morpheus-consumer-node.RAILWAY_PRIVATE_DOMAIN}}:8082
NODE_USERNAME=admin
NODE_PASSWORD=${{morpheus-consumer-node.NODE_PASSWORD}}
HELPER_URL=http://${{morpheus-consumer-node.RAILWAY_PRIVATE_DOMAIN}}:8083
HELPER_TOKEN=${{morpheus-consumer-node.HELPER_TOKEN}}
RPC_URL=${{morpheus-consumer-node.ETH_NODE_ADDRESS}}
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

The Raw Editor accepts `NAME=value` lines and may display values with quotes. Those displayed quotes are normal dotenv formatting. When editing an individual variable's value field, enter only the value, without `NAME=` or literal surrounding quotes. Railway's reference autocomplete can help select the correct service.

### Check the resolved values before redeploying

A reference can look correctly written in the Raw Editor and still point at a nonexistent service or empty source variable. Inspect the resolved values privately on the gateway:

| Gateway variable | Expected resolved value |
| --- | --- |
| `NODE_URL` | `http://morpheus-consumer-node.railway.internal:8082`, using your actual node hostname |
| `HELPER_URL` | Same node hostname with `http://` and port `8083` |
| `NODE_PASSWORD` | Populated, matching the node password; never `<empty string>` |
| `HELPER_TOKEN` | Populated, matching the node helper token |
| `RPC_URL` | The node's full Base RPC URL, including any required provider credential |

During the walkthrough, `HELPER_URL` resolved to `http://:8083` and `NODE_PASSWORD` to an empty string. That is a reference-resolution/configuration problem; the gateway fails before it can try helper authentication. Verify the actual node service name, same project environment, populated source variables and applied changes. Waiting for another restart does not repair a wrong reference.

If needed, bypass just the hostname reference by copying the node's Private Networking hostname into the two URL values. For the service names used here:

```dotenv
NODE_URL=http://morpheus-consumer-node.railway.internal:8082
HELPER_URL=http://morpheus-consumer-node.railway.internal:8083
```

This does not repair empty password/token references; correct those separately. The helper URL contains only the scheme, hostname and port. Keep credentials in their separate variables and omit paths such as `/healthz`, `/status` or `/v1`.

Leave `HELPER_SOCKET` unset on Railway. `HELPER_URL` selects the network transport; setting both deliberately fails startup. Also leave `ADMIN_TOKEN_FILE`, `NODE_PASSWORD_FILE`, `HELPER_TOKEN_FILE`, `WALLET_PRIVATE_KEY_FILE` and `RPC_URL_FILE` unset when supplying values through Variables. File variables take precedence and would point at nonexistent Docker Compose secret mounts.

### Generate the web domain and set the browser origin

Open **gateway → Settings → Networking → Public Networking** and click **Generate Domain**, targeting port **8000**. Choose an HTTP domain for the dashboard/API. A **TCP Proxy** address such as `something.proxy.rlwy.net:53059` is not the HTTPS web domain used by this guide. If you created one accidentally, remove that gateway TCP proxy and generate a domain; Railway documents removing the proxy if it hides the Generate Domain option. Leave the node private. [Railway domain setup](https://docs.railway.com/networking/domains/working-with-domains)

Copy the generated domain and set the gateway's origin with **`https://`**. For the walkthrough's domain, the working value was:

```dotenv
PUBLIC_ORIGIN=https://morpheus-consumer-gateway-production.up.railway.app
COOKIE_SECURE=true
```

Use your own generated domain for a new installation. Railway may display just the hostname, and a browser may hide the scheme in its address bar; `PUBLIC_ORIGIN` still needs the explicit `https://` prefix. Do not use `http://`, the internal `.railway.internal` hostname, a TCP proxy port, `/v1`, or a trailing slash. Port 8000 is Railway's internal target, not a port to append to this public HTTPS URL. A browser URL ending in `/` is normal; omit that slash in the configured origin.

Apply the Variables, deploy the staged changes and wait for the new gateway deployment. Refresh the browser before logging in. Merely editing the Raw Editor without applying/deploying does not change the running process. The login error **“Administrative changes require the configured browser origin”** means the browser's origin does not match this setting; correct the origin before investigating the admin password. Keep `COOKIE_SECURE=true` for HTTPS. A custom domain works too, with its exact HTTPS origin configured.

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

In your computer's macOS/Linux terminal using **zsh or Bash**, replace the base URL below with your gateway's public HTTPS URL, then run this block. Paste the application key at the prompt; input is hidden and the key is not embedded in the command history. The `read -s` option is for these shells, not generic POSIX `sh` or PowerShell.

```bash
printf 'API key: '
read -rs MORPHEUS_API_KEY
printf '\n'
MORPHEUS_BASE_URL=https://YOUR-GATEWAY-DOMAIN/v1

curl --fail-with-body "$MORPHEUS_BASE_URL/models" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY"

curl --fail-with-body --max-time 360 "$MORPHEUS_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with: Morpheus is working!"}],"max_tokens":2048}'
```

The operator confirmed that this non-streaming request worked against `https://morpheus-consumer-gateway-production.up.railway.app/v1`. That is the walkthrough installation, not a shared endpoint for new users. The model alias must already be saved in your dashboard and included in the application's key scope. Look for the assistant reply in the returned JSON.

The first prompt can take longer because the node negotiates a provider and opens an on-chain session. Session opening escrows MOR and uses ETH for gas. Keep **Sessions** open in another tab so the viewer can see the session appear.

Test streaming through Railway's actual public URL:

```sh
curl -N --fail-with-body --max-time 360 "$MORPHEUS_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $MORPHEUS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"Explain consumer nodes in two sentences."}],"max_tokens":2048}'
```

Expect incremental `data:` events ending in `data: [DONE]`. If a stream has already begun, a subsequent error is carried in the stream and may still have HTTP status 200. Inspect the events, not just curl's exit status. Do not automatically replay a timed-out prompt: inspect Sessions first because its opening may already have mined.

When finished with both requests, clear the shell variables:

```sh
unset MORPHEUS_API_KEY MORPHEUS_BASE_URL
```

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
| Temporarily override gateway `HELPER_TOKEN` with a different generated value | `/readyz` fails and management actions cannot execute. Restore `${{morpheus-consumer-node.HELPER_TOKEN}}` and redeploy to recover. |
| Close a test session | It becomes closed after chain confirmation. Repeating close does not open another session. |
| Recovery after withdrawal eligibility | Eligible MOR is withdrawn if policy and gas reserve permit; day-locked MOR waits for the contract's release window. |

Before testing redeploys, disable keep-available replacement, pause admissions and wait for active requests to finish. Restore normal settings afterward. Never delete persistent volumes or unresolved operations to clear an error.

## 8. Video sequence

1. Show the two published image names, actual service names and separate service cards.
2. Attach both volumes. Explain the deployment secrets with placeholders; enter real values off-camera.
3. Configure and deploy the node. If creation triggered a premature failed attempt, explain the missing settings, apply them and redeploy.
4. Copy the private hostname from the **node** card. Configure the gateway references with the actual node service name. Show the resolved hostname; keep resolved credentials hidden.
5. Generate an HTTPS domain on the **gateway**, target 8000, set the exact `https://` origin and redeploy. Explain that this is different from creating a TCP proxy.
6. Open the domain, log in and verify the wallet/network. If origin rejection appears, show the scheme/domain correction with credentials hidden.
7. Add DeepSeek, choose 30 minutes and save provider/rating settings.
8. Create an application key off-camera and paste it at the terminal's hidden prompt.
9. Send the first curl request and show its reply/session. Send another request to demonstrate reuse, checking the session ID.
10. After validating those paths in your deployment, demonstrate streaming and a node restart from the UI.
11. Close the test session and explain liquid, held and withdrawable MOR. Revoke any keys exposed during recording.

## 9. Troubleshooting and maintenance

| Symptom | Check |
| --- | --- |
| Image pull denied / manifest unknown | Confirm the tag was published and the namespace is correct. Configure registry credentials for private images. Docker Hub credentials are unrelated to `NODE_PASSWORD`. |
| Node reports missing wallet/password | Set node Variables with values, not `_FILE` paths copied from Compose. Check the password is at least 32 characters. |
| Startup fails with helper token error | Generate a URL-safe 32–256 character token; use the same value through the gateway's reference variable. |
| `HELPER_URL must be an HTTP(S) origin without credentials or a path` | Inspect the resolved value. `http://:8083` has no hostname. Fix the referenced service name/source value, or use the node's actual private hostname with `http://` and port 8083. Omit credentials and endpoint paths. |
| References look correct, but password is `<empty string>` | Check the actual node service name and its populated source variables in the same environment. Apply source changes and redeploy affected services. Re-pasting `${{node.NODE_PASSWORD}}` cannot work when the service is named `morpheus-consumer-node`. |
| Only `something.proxy.rlwy.net:PORT` is shown publicly | A TCP proxy was created. Remove the accidental gateway proxy and use **Generate Domain** targeting 8000 for HTTPS. |
| `/readyz` is 503 but `/healthz` is 200 | Check node RPC/wallet readiness, private URLs, helper token and helper readiness. Gateway's admin status shows an actionable helper authentication error. |
| Permission denied on `/data` or `/node-data` | Confirm the volume path, `RAILWAY_RUN_UID=0` and the unchanged image entrypoint. Applications should run as UID 10001 after initialization. |
| “Administrative changes require the configured browser origin” at login or Save | Set `PUBLIC_ORIGIN=https://YOUR-GATEWAY-DOMAIN`, with HTTPS even if the address bar hides it. Omit path, trailing slash and internal target port. Apply, redeploy gateway and refresh; keep `COOKIE_SECURE=true`. |
| Private connection refused | Verify same Railway environment, `:8082` on the node API, helper `::` on 8083 and the exact service name in references. |
| Healthcheck points at the wrong port | Node `PORT=8083`, gateway `PORT=8000`; health paths differ. |
| Native transaction history fails | For image `railway-20260914.1`, explicitly set `BLOCKSCOUT_API_URL=https://base.blockscout.com/api/v2`; its baked-in explorer default is missing `/v2`. |
| curl returns 401 or unknown model | Use the dashboard-created application key, not `ADMIN_TOKEN`; check revocation, model scope and that the exact alias is saved. |
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

The new automated tests exercise authenticated HTTP management over real TCP, unauthorized access, restart idempotency, journal reads, response/body limits, IPv4/IPv6 sockets and readiness with a bad helper token. A local smoke deployment using `deploy/railway-smoke.compose.yaml` reproduces separate containers, independent root-owned volumes and private HTTP without a shared control socket.

**Subsequent source correction:** Commit `bb4f62a` corrects the Blockscout defaults to `/api/v2` and passes optional contract/explorer overrides from Compose `.env`. Eighteen affected helper/transport tests passed, and a read-only call verified the Base V2 transaction endpoint's response format. The published `.1` images were not rebuilt for that change; section 3 supplies the explicit explorer variable needed by that image.

**Railway walkthrough, operator-reported:** On September 14, 2026, the operator confirmed node startup, successful dashboard login after correcting the HTTPS origin, and a successful non-streaming DeepSeek curl request from their computer. Screenshots showed the intermediate empty hostname/password references, accidental TCP proxy and browser-origin rejection documented above. We did not independently collect Railway transaction receipts or rerun the complete acceptance suite against that public deployment. Streaming, concurrent inference, Railway restarts/redeploy persistence and recovery/withdrawal remain section 7 acceptance checks; local results are not represented as Railway results. No real admin token, application key, wallet key or credential-bearing RPC URL is included in this guide.
