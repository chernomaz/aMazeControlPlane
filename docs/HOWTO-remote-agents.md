# HOWTO: Running Agents and MCP Servers on a Remote Machine

This guide explains how to run aMaze agents and MCP servers on a **separate
machine** from the one running the aMaze platform (proxy, orchestrator, UI,
Redis).

## Topology

```
PUBLIC MACHINE (e.g. 51.20.64.153 — AWS EC2)
  aMaze platform:
    :8001  orchestrator   (agent/MCP registration, policy)
    :8080  mitmproxy      (all agent traffic goes through here)
    :5173  UI
    :16686 Jaeger

LOCAL MACHINE (e.g. 192.168.1.131 — behind NAT)
  Your agents and MCP servers:
    :9005  agent-a  (A2A port)
    :9006  agent-b  (A2A port)
    :8002  my-mcp   (MCP server)
```

The local machine is behind NAT — the public machine cannot reach it directly.
We use an **SSH reverse tunnel** so the aMaze proxy on the public machine can
call back into the local machine's agent A2A endpoints and MCP server.

---

## Part 1 — Prerequisites

### 1.1 Install Docker Compose V2

Docker Compose V1 (`docker-compose`) is too old. Install the V2 plugin:

```bash
# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y docker-compose-plugin

# If the package is not found (Docker not from official repo):
mkdir -p ~/.docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/download/v2.27.1/docker-compose-linux-x86_64 \
  -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose
```

Verify:
```bash
docker compose version   # must print v2.x.x
```

### 1.2 Add your user to the docker group

```bash
sudo usermod -aG docker $USER
newgrp docker            # apply without logout
docker ps                # must work without sudo
```

### 1.3 Get the project

```bash
git clone <your-repo-url>
cd aMaze
```

### 1.4 Create a .env file

```bash
# aMaze/.env
OPENAI_API_KEY=sk-...        # required if agents use OpenAI
TAVILY_API_KEY=tvly-...      # optional, for web_search tool
# add any keys your custom MCP tools need
```

---

## Part 2 — Configure the Public Machine

All commands in this section run on the **public machine** (e.g. 51.20.64.153).

### 2.1 Open firewall ports (AWS Security Group)

The agents on the local machine need to reach two ports on the public machine:

| Port | Service |
|------|---------|
| 8001 | Orchestrator (registration) |
| 8080 | Proxy (all agent traffic) |

In the AWS Console:
1. EC2 → Instances → your instance → Security Group
2. Edit Inbound Rules → Add:
   - TCP 8001 from `0.0.0.0/0`
   - TCP 8080 from `0.0.0.0/0`

### 2.2 Enable GatewayPorts in sshd

This allows the SSH reverse tunnel to bind on all network interfaces (not just
loopback), so Docker containers on the public machine can reach tunnel ports.

```bash
sudo nano /etc/ssh/sshd_config
# find the line: #GatewayPorts no
# change it to:   GatewayPorts yes

sudo systemctl restart ssh     # Ubuntu uses "ssh", not "sshd"

# Verify
sudo sshd -T | grep gatewayports
# must print: gatewayports yes
```

### 2.3 Start the aMaze platform

```bash
cd /path/to/aMaze
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps
# all three containers (amaze-redis, amaze-platform, amaze-ui) must show Up
```

Verify:
```bash
curl http://localhost:8001/agents    # orchestrator responds
```

UI: http://<public-ip>:5173

---

## Part 3 — Set Up the SSH Reverse Tunnel

Run this on the **local machine** (192.168.1.131). Keep the terminal open —
the tunnel must stay up while agents are running.

```bash
# Basic tunnel (will drop if connection is interrupted)
ssh -i /path/to/aws-key.pem -N \
    -R 0.0.0.0:9005:localhost:9005 \
    -R 0.0.0.0:9006:localhost:9006 \
    -R 0.0.0.0:8002:localhost:8002 \
    ubuntu@<public-ip>
```

For a **persistent tunnel** that auto-reconnects (recommended):

```bash
sudo apt-get install -y autossh

autossh -M 0 -N \
    -o "ServerAliveInterval 30" \
    -o "ServerAliveCountMax 3" \
    -o "ExitOnForwardFailure yes" \
    -i /path/to/aws-key.pem \
    -R 0.0.0.0:9005:localhost:9005 \
    -R 0.0.0.0:9006:localhost:9006 \
    -R 0.0.0.0:8002:localhost:8002 \
    ubuntu@<public-ip>
```

**Verify the tunnel is working** (run on public machine):

```bash
ss -tlnp | grep 8002
# must show: 0.0.0.0:8002   (NOT 127.0.0.1 — that means GatewayPorts didn't apply)
```

> **Important:** If `ss` shows `127.0.0.1:8002`, GatewayPorts is not active.
> Restart the SSH service on the public machine and reopen the tunnel.

Port mapping summary:

| Local port | Tunneled to public machine port | Purpose |
|------------|-------------------------------|---------|
| 9005 | 51.20.64.153:9005 | Agent A — A2A |
| 9006 | 51.20.64.153:9006 | Agent B — A2A |
| 8002 | 51.20.64.153:8002 | MCP server |

Add more `-R` lines for additional agents or MCP servers.

---

## Part 4 — Proxy CA Certificate (automatic)

The aMaze SDK fetches the mitmproxy CA certificate automatically from
`GET /ca.pem` on the orchestrator when the agent starts. No manual step
needed for remote agents.

The SDK installs the cert before any HTTPS traffic is made:
```
Agent starts → SDK calls GET <orchestrator>/ca.pem
            → writes cert to /tmp/amaze-ca-*.pem
            → sets SSL_CERT_FILE + REQUESTS_CA_BUNDLE
            → registers with orchestrator (now trusts the proxy)
```

Co-resident agents (same machine as platform) continue to use the Docker
volume mount as before — the SDK skips the fetch if `SSL_CERT_FILE` is
already set.

---

## Part 5 — Create Your MCP Server

### 5.1 Directory structure

```
examples/my_mcp/
├── server.py          ← your MCP server (FastMCP)
├── tools/             ← one .py file per tool
│   ├── tool_a.py
│   └── tool_b.py
├── requirements.txt
├── Dockerfile
└── entrypoint.sh
```

### 5.2 server.py pattern

Use `server.py` from `examples/mcp_server/` as a template — it auto-discovers
tools from the `tools/` directory. Change only the name and port:

```python
mcp = FastMCP("my-mcp")   # logical name

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))   # use 8002 (or your chosen port)
    mcp.run(transport="streamable-http", host=host, port=port)
```

### 5.3 Dockerfile

Copy from `examples/mcp_server/Dockerfile` and change two things:

```dockerfile
# Change all path references from mcp_server → my_mcp
COPY examples/my_mcp/requirements.txt /mcp/requirements.txt
COPY examples/my_mcp/ /mcp/

# Change the port
ENV HOST=0.0.0.0 \
    PORT=8002

EXPOSE 8002
```

### 5.4 entrypoint.sh

Same as `examples/mcp_server/entrypoint.sh` — no changes needed:

```sh
#!/bin/sh
if [ -n "$AMAZE_ORCHESTRATOR_URL" ]; then
    python /usr/local/bin/amaze-mcp-register &
fi
exec python /mcp/server.py
```

---

## Part 6 — Create Your Agents

### 6.1 Agent file pattern

Place your agent `.py` files in `examples/agents/`. Minimum required structure:

```python
import amaze

async def receive_message_from_user(q) -> str:
    # your logic here — call LLM, call MCP tools, etc.
    return "reply"

async def receive_message_from_agent(caller: str, q) -> str:
    # handle A2A calls from other agents
    return await receive_message_from_user(q)

if __name__ == "__main__":
    amaze.init()    # starts A2A + chat servers, registers with orchestrator
```

The existing `Dockerfile` (`examples/agents/Dockerfile`) copies all `*.py`
files automatically — no Dockerfile changes needed for new agents.

### 6.2 MCP URL in agent code

Agents on the local machine connect to MCP servers **through the proxy** on
the public machine. Use the public machine IP and tunneled port:

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "tools": {
        "url": "http://<public-ip>:8002/mcp/",   # tunneled MCP
        "transport": "streamable_http",
    }
})
```

---

## Part 7 — Docker Compose for the Local Machine

Create `examples/my-compose.yml` on the **local machine**. Note:
- `context: ..` — build context is the repo root (Dockerfiles use `COPY examples/...`)
- `AMAZE_A2A_HOST` uses the **public IP** (tunnel endpoint)
- `AMAZE_MCP_URL` uses `host.docker.internal` so the orchestrator container
  on the public machine can probe it through the tunnel

```yaml
name: amaze-agents

services:

  my-mcp:
    build:
      context: ..
      dockerfile: examples/my_mcp/Dockerfile
    container_name: my-mcp
    restart: unless-stopped
    ports:
      - "8002:8002"
    environment:
      HOST: 0.0.0.0
      PORT: "8002"
      AMAZE_ORCHESTRATOR_URL: http://<public-ip>:8001
      AMAZE_MCP_NAME: my-mcp
      # host.docker.internal → public machine's host gateway, reachable from
      # inside the orchestrator Docker container via the SSH tunnel port
      AMAZE_MCP_URL: http://host.docker.internal:8002/mcp
      AMAZE_REGISTER_RETRIES: "20"
      AMAZE_REGISTER_DELAY: "3"

  agent-a:
    build:
      context: ..
      dockerfile: examples/agents/Dockerfile
      args:
        AGENT_MODULE: my_agent_a       # filename without .py
    container_name: agent-a
    restart: unless-stopped
    environment:
      HTTP_PROXY:  http://<public-ip>:8080
      HTTPS_PROXY: http://<public-ip>:8080
      NO_PROXY: <public-ip>
      AMAZE_ORCHESTRATOR_URL: http://<public-ip>:8001
      AMAZE_PROXY_URL:        http://<public-ip>:8080
      AMAZE_AGENT_ID:  agent-a
      AMAZE_A2A_HOST:  <public-ip>    # proxy calls back via tunnel
      AMAZE_A2A_PORT:  "9005"
      OPENAI_API_KEY:  ${OPENAI_API_KEY}
      # No SSL_CERT_FILE needed — SDK fetches the CA cert from the
      # orchestrator automatically on startup
    ports:
      - "9005:9005"

  agent-b:
    build:
      context: ..
      dockerfile: examples/agents/Dockerfile
      args:
        AGENT_MODULE: my_agent_b
    container_name: agent-b
    restart: unless-stopped
    environment:
      HTTP_PROXY:  http://<public-ip>:8080
      HTTPS_PROXY: http://<public-ip>:8080
      NO_PROXY: <public-ip>
      AMAZE_ORCHESTRATOR_URL: http://<public-ip>:8001
      AMAZE_PROXY_URL:        http://<public-ip>:8080
      AMAZE_AGENT_ID:  agent-b
      AMAZE_A2A_HOST:  <public-ip>
      AMAZE_A2A_PORT:  "9006"
      OPENAI_API_KEY:  ${OPENAI_API_KEY}
    ports:
      - "9006:9006"
```

Replace `<public-ip>` with your actual public machine IP (e.g. `51.20.64.153`).

---

## Part 8 — Configure Policies

On the **public machine**, edit `config/policies.yaml`:

```yaml
policies:
  agent-a:
    mode: flexible
    max_tokens_per_turn: 10000
    max_tool_calls_per_turn: 20
    max_agent_calls_per_turn: 5
    allowed_llm_providers: [openai]
    allowed_tools: [tool_a, tool_b]    # exact tool names from your MCP server
    allowed_agents: [agent-b]          # agents this agent may call via A2A
    on_violation: block
    on_budget_exceeded: block

  agent-b:
    mode: flexible
    max_tokens_per_turn: 10000
    max_tool_calls_per_turn: 20
    max_agent_calls_per_turn: 5
    allowed_llm_providers: [openai]
    allowed_tools: [tool_a, tool_b]
    allowed_agents: []
    on_violation: block
    on_budget_exceeded: block
```

Restart the platform to reload policies:

```bash
docker compose -f docker/docker-compose.yml restart amaze
```

---

## Part 9 — Start Everything

On the **local machine**:

```bash
cd /path/to/aMaze/examples

# Start MCP server first and confirm it registers
docker compose -f my-compose.yml up --build my-mcp

# Watch for: "OK — 'my-mcp' registered with N tool(s): ..."
# Then Ctrl+C and start everything together:

docker compose -f my-compose.yml up --build -d
```

Check all containers are running:

```bash
docker compose -f my-compose.yml ps
docker logs agent-a --tail 20
docker logs agent-b --tail 20
docker logs my-mcp  --tail 20
```

---

## Part 10 — Approve in the UI

Open the UI on the public machine: **http://\<public-ip\>:5173**

1. **Agents** tab → find `agent-a` and `agent-b` → click **Approve**
2. **MCP Servers** tab → find `my-mcp` → click **Approve** (if not already approved)

Or via API:

```bash
curl -X POST http://<public-ip>:8001/agents/agent-a/approve
curl -X POST http://<public-ip>:8001/agents/agent-b/approve
curl -X POST http://<public-ip>:8001/mcp_servers/my-mcp/approve
```

---

## Part 11 — Verify

**Check policies loaded:**
```bash
curl http://<public-ip>:8001/policy/agent-a | python3 -m json.tool
```

**Send a test message** (if agent-a exposes a chat port):
```bash
curl -X POST http://localhost:<chat-port>/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "hello"}'
```

**Check audit log** (on public machine):
```bash
docker exec amaze-redis redis-cli XRANGE "audit:agent-a" - + COUNT 5
```

**Check traces:** http://\<public-ip\>:16686 (Jaeger)

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `permission denied` on docker commands | User not in docker group | `sudo usermod -aG docker $USER && newgrp docker` |
| `unknown command: docker compose` | Compose V2 not installed | See Part 1.1 |
| MCP registration: `Cannot reach orchestrator` | Port 8001 not open | Open port 8001 in AWS Security Group |
| MCP probe: `All connection attempts failed` | Tunnel on loopback only | Enable `GatewayPorts yes`, restart ssh, reopen tunnel |
| Tunnel shows `127.0.0.1:8002` not `0.0.0.0:8002` | `GatewayPorts` not active | `sudo sshd -T \| grep gatewayports` — must be yes |
| UI shows stale tool list after MCP restart | Old Redis entry | `docker exec amaze-redis redis-cli DEL mcp:<name>` then restart MCP |
| Agent TLS errors through proxy | CA cert missing | See Part 4 |
| Tunnel drops | No keepalive | Use `autossh` instead of plain `ssh` |
| `Permission denied (publickey)` | SSH key not specified | Add `-i /path/to/key.pem` to ssh command |
