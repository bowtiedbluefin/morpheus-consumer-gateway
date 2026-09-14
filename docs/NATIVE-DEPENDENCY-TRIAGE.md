# Native dependency call-graph triage

The actual deployed binary produced 53 symbol-level advisory matches in 18 modules. A separate source scan of the patched upstream checkout, using the `docker` build tag, Linux/ARM64 and Go 1.25.14, completed with **32 advisory matches in 19 modules**. It also reported 24 imported-package and 17 required-module advisories without an apparent call path. These are separate analyses, not additive vulnerability counts.

Static call paths are conservative: several pass through initialization, formatting interfaces or generic synchronization callbacks. They do not establish that an unauthenticated customer can trigger the vulnerable behavior in this configuration. Conversely, a missing static path is not proof of safety. Remediation needs dependency upgrades/removal and feature-specific reachability review. `N/A` means the scan did not provide a fixed version; it does not mean no remediation exists. See [govulncheck limitations](https://pkg.go.dev/golang.org/x/vuln/cmd/govulncheck).

The full 1.49 MB source trace is retained locally in `data/production-readiness/native-source-vulnerability-scan.txt`. The [binary report](NATIVE-VULNERABILITY-SCAN.txt) is committed. The table below is a remediation inventory from the source scan.

| Advisory | Found | Fixed version reported |
| --- | --- | --- |
| [GO-2026-6165](https://pkg.go.dev/vuln/GO-2026-6165) | `github.com/pion/dtls/v3@v3.0.4` | `github.com/pion/dtls/v3@v3.1.4` |
| [GO-2026-6162](https://pkg.go.dev/vuln/GO-2026-6162) | `github.com/sigstore/sigstore-go@v1.0.0` | `github.com/sigstore/sigstore-go@v1.2.1` |
| [GO-2026-6099](https://pkg.go.dev/vuln/GO-2026-6099) | `github.com/quic-go/webtransport-go@v0.8.1-0.20241018022711-4ac2c9250e66` | `github.com/quic-go/webtransport-go@v0.11.1` |
| [GO-2026-5970](https://pkg.go.dev/vuln/GO-2026-5970) | `golang.org/x/text@v0.35.0` | `golang.org/x/text@v0.39.0` |
| [GO-2026-5952](https://pkg.go.dev/vuln/GO-2026-5952) | `github.com/sigstore/sigstore-go@v1.0.0` | `github.com/sigstore/sigstore-go@v1.2.0` |
| [GO-2026-5932](https://pkg.go.dev/vuln/GO-2026-5932) | `golang.org/x/crypto@v0.49.0` | `N/A` |
| [GO-2026-5851](https://pkg.go.dev/vuln/GO-2026-5851) | `github.com/sigstore/timestamp-authority@v1.2.7` | `N/A` |
| [GO-2026-5778](https://pkg.go.dev/vuln/GO-2026-5778) | `github.com/sigstore/rekor@v1.3.10` | `github.com/sigstore/rekor@v1.5.2` |
| [GO-2026-5763](https://pkg.go.dev/vuln/GO-2026-5763) | `github.com/sigstore/timestamp-authority@v1.2.7` | `N/A` |
| [GO-2026-5684](https://pkg.go.dev/vuln/GO-2026-5684) | `github.com/ipld/go-ipld-prime@v0.21.0` | `github.com/ipld/go-ipld-prime@v0.23.0` |
| [GO-2026-5676](https://pkg.go.dev/vuln/GO-2026-5676) | `github.com/quic-go/quic-go@v0.50.0` | `github.com/quic-go/quic-go@v0.59.1` |
| [GO-2026-5547](https://pkg.go.dev/vuln/GO-2026-5547) | `github.com/in-toto/in-toto-golang@v0.9.0` | `github.com/in-toto/in-toto-golang@v0.11.0` |
| [GO-2026-5506](https://pkg.go.dev/vuln/GO-2026-5506) | `go.opentelemetry.io/otel@v1.39.0` | `go.opentelemetry.io/otel@v1.41.0` |
| [GO-2026-5073](https://pkg.go.dev/vuln/GO-2026-5073) | `github.com/ipld/go-ipld-prime@v0.21.0` | `github.com/ipld/go-ipld-prime@v0.22.0` |
| [GO-2026-5026](https://pkg.go.dev/vuln/GO-2026-5026) | `golang.org/x/net@v0.52.0` | `golang.org/x/net@v0.55.0` |
| [GO-2026-4945](https://pkg.go.dev/vuln/GO-2026-4945) | `github.com/go-jose/go-jose/v4@v4.1.3` | `github.com/go-jose/go-jose/v4@v4.1.4` |
| [GO-2026-4887](https://pkg.go.dev/vuln/GO-2026-4887) | `github.com/docker/docker@v28.0.4+incompatible` | `N/A` |
| [GO-2026-4883](https://pkg.go.dev/vuln/GO-2026-4883) | `github.com/docker/docker@v28.0.4+incompatible` | `N/A` |
| [GO-2026-4488](https://pkg.go.dev/vuln/GO-2026-4488) | `github.com/quic-go/webtransport-go@v0.8.1-0.20241018022711-4ac2c9250e66` | `github.com/quic-go/webtransport-go@v0.10.0` |
| [GO-2026-4485](https://pkg.go.dev/vuln/GO-2026-4485) | `github.com/quic-go/webtransport-go@v0.8.1-0.20241018022711-4ac2c9250e66` | `github.com/quic-go/webtransport-go@v0.10.0` |
| [GO-2026-4483](https://pkg.go.dev/vuln/GO-2026-4483) | `github.com/quic-go/webtransport-go@v0.8.1-0.20241018022711-4ac2c9250e66` | `github.com/quic-go/webtransport-go@v0.10.0` |
| [GO-2026-4479](https://pkg.go.dev/vuln/GO-2026-4479) | `github.com/pion/dtls/v2@v2.2.12` | `N/A` |
| [GO-2026-4377](https://pkg.go.dev/vuln/GO-2026-4377) | `github.com/theupdateframework/go-tuf/v2@v2.1.1` | `github.com/theupdateframework/go-tuf/v2@v2.4.1` |
| [GO-2026-4355](https://pkg.go.dev/vuln/GO-2026-4355) | `github.com/sigstore/rekor@v1.3.10` | `github.com/sigstore/rekor@v1.5.0` |
| [GO-2026-4354](https://pkg.go.dev/vuln/GO-2026-4354) | `github.com/sigstore/rekor@v1.3.10` | `github.com/sigstore/rekor@v1.5.0` |
| [GO-2026-4349](https://pkg.go.dev/vuln/GO-2026-4349) | `github.com/theupdateframework/go-tuf/v2@v2.1.1` | `github.com/theupdateframework/go-tuf/v2@v2.3.1` |
| [GO-2026-4348](https://pkg.go.dev/vuln/GO-2026-4348) | `github.com/theupdateframework/go-tuf/v2@v2.1.1` | `github.com/theupdateframework/go-tuf/v2@v2.3.1` |
| [GO-2026-4316](https://pkg.go.dev/vuln/GO-2026-4316) | `github.com/go-chi/chi@v4.1.2+incompatible` | `N/A` |
| [GO-2025-4233](https://pkg.go.dev/vuln/GO-2025-4233) | `github.com/quic-go/quic-go@v0.50.0` | `github.com/quic-go/quic-go@v0.57.0` |
| [GO-2025-4192](https://pkg.go.dev/vuln/GO-2025-4192) | `github.com/sigstore/timestamp-authority@v1.2.7` | `N/A` |
| [GO-2025-3787](https://pkg.go.dev/vuln/GO-2025-3787) | `github.com/go-viper/mapstructure/v2@v2.2.1` | `github.com/go-viper/mapstructure/v2@v2.3.0` |
| [GO-2024-3218](https://pkg.go.dev/vuln/GO-2024-3218) | `github.com/libp2p/go-libp2p-kad-dht@v0.30.2` | `N/A` |

## Interpreting the two scans

Source-only advisory IDs: GO-2024-3218, GO-2025-4192, GO-2026-4316, GO-2026-4348, GO-2026-4349, GO-2026-4354, GO-2026-4355, GO-2026-4377, GO-2026-4479, GO-2026-4483, GO-2026-4485, GO-2026-4488, GO-2026-5547, GO-2026-5763, GO-2026-5778, GO-2026-5851, GO-2026-5952, GO-2026-6099, GO-2026-6162.

Binary-only advisory IDs: GO-2022-1098, GO-2024-2818, GO-2024-3189, GO-2025-3735, GO-2025-3748, GO-2025-3900, GO-2025-4017, GO-2026-4358, GO-2026-4610, GO-2026-4918, GO-2026-5005, GO-2026-5006, GO-2026-5013, GO-2026-5014, GO-2026-5015, GO-2026-5016, GO-2026-5017, GO-2026-5018, GO-2026-5019, GO-2026-5020, GO-2026-5021, GO-2026-5023, GO-2026-5025, GO-2026-5027, GO-2026-5028, GO-2026-5029, GO-2026-5030, GO-2026-5033, GO-2026-5327, GO-2026-5617, GO-2026-5668, GO-2026-5746, GO-2026-5841, GO-2026-5942, GO-2026-6163, GO-2026-6179, GO-2026-6180, GO-2026-6303, GO-2026-6354, GO-2026-6355.

Neither list establishes remote exploitability. Source inspection includes compiled packages that the linker can discard; the binary does not retain a source call graph and may include unreachable symbols. Both findings sets require triage against the actual feature configuration.
