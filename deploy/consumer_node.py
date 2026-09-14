"""Remove unused host-management features from the pinned consumer-only build.

The gateway needs remote inference, wallet/session APIs and TEE verification.
It does not expose IPFS file management or Docker host administration. Removing
their handlers and implementations also removes the Kubo/P2P/Docker daemon
dependency graph; merely disabling configuration would leave that code linked.
"""

import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
controller = root / "internal/proxyapi/controller_http.go"
text = controller.read_text()
handlers = (
    "Pin",
    "Unpin",
    "DownloadFile",
    "StreamDownloadFile",
    "AddFile",
    "GetIpfsVersion",
    "GetPinnedFiles",
    "BuildDockerImage",
    "StreamBuildDockerImage",
    "StartContainer",
    "StopContainer",
    "RemoveContainer",
    "GetContainer",
    "ListContainers",
    "GetContainerLogs",
    "StreamContainerLogs",
    "GetDockerVersion",
    "PruneImages",
    "PruneContainers",
)
for name in handlers:
    text, count = re.subn(r"(?ms)^func \(c \*ProxyController\) " + name + r"\(.*?^}\n", "", text)
    if count != 1:
        raise SystemExit(f"Pinned consumer handler mismatch: {name}")
text = re.sub(r'(?m)^\s*r\.(GET|POST)\("/(ipfs|docker)/.*\n', "", text)
for line in (
    '\t"bufio"\n',
    '\t"errors"\n',
    '\t"strconv"\n',
    '\t"time"\n',
    "\tdockerManager := NewDockerManager(log)\n",
):
    if line not in text:
        raise SystemExit(f"Pinned consumer source mismatch: {line.strip()}")
    text = text.replace(line, "")
text = re.sub(r"(?m)^\s*dockerManager\s+\*DockerManager\n", "", text)
text = re.sub(r"(?m)^\s*dockerManager:\s+dockerManager,\n", "", text)
controller.write_text(text)
requests = root / "internal/proxyapi/requests.go"
text, count = re.subn(r"(?s)// DockerBuildReq .*?(?=type CallAgentToolReq)", "", requests.read_text())
if count != 1:
    raise SystemExit("Pinned Docker request types mismatch")
requests.write_text(text)
(root / "internal/proxyapi/docker_manager.go").unlink()
(root / "internal/proxyapi/ipfs_manager.go").write_text("""package proxyapi

import "github.com/MorpheusAIs/Morpheus-Lumerin-Node/proxy-router/internal/lib"

// Constructor compatibility only. No IPFS or Docker handlers exist in this build.
type IpfsManager struct{}
func NewIpfsManagerDisabled(log lib.ILogger) *IpfsManager { return &IpfsManager{} }
func NewIpfsManager(_ string, log lib.ILogger) *IpfsManager { return NewIpfsManagerDisabled(log) }
""")
