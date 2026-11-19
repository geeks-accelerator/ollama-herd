# Running Herd in Docker

`pip install ollama-herd` is still the main way to run Herd, and the only way for Mac and Windows
nodes. The container image is for the router, and for nodes on Linux machines:

```
ghcr.io/geeks-accelerator/ollama-herd
```

Tags: `X.Y.Z` for each release, `X.Y` for the latest patch of a minor, and `latest`. Images are
built for `linux/amd64` and `linux/arm64`, from the release's committed `uv.lock`, and each one is
tested before it is pushed: a router container and a node container find each other and route a
real chat request (`scripts/docker-smoke.sh`). Published from the first release after 0.12.6.

One image runs both programs: `herd` by default, `herd-node` when given as the command. Ollama is
not in the image and stays on the host.

## Where it fits

| Role | In a container? | Why |
|---|---|---|
| Router (`herd`) on a Linux server or NAS | **Yes** | One port and a data volume |
| Router on Mac or Windows (Docker Desktop) | Works | Nodes cannot discover it over mDNS: give each node its URL |
| Node (`herd-node`) on Linux | **Yes, with `--network host`** | Discovery, the address the node reports and the Ollama LAN proxy all need the host's network |
| Node on a Mac | **No** | Docker on a Mac is a Linux VM. The node reports the VM's memory and cores, not the Mac's: tested on a 128 GB Mac, it reported 7.65 GB and the router refused to send it anything. No Metal, no MLX |
| Node on Windows | **No** | The same VM problem under Docker Desktop. `herd-node` runs natively on Windows |

## The router

On a Linux machine, use the host network so nodes can discover the router over mDNS:

```bash
docker run -d --name herd --network host --restart unless-stopped \
  -v herd-data:/home/herd/.fleet-manager \
  ghcr.io/geeks-accelerator/ollama-herd
```

Anywhere else, or if you prefer a published port:

```bash
docker run -d --name herd -p 11435:11435 --restart unless-stopped \
  -v herd-data:/home/herd/.fleet-manager \
  ghcr.io/geeks-accelerator/ollama-herd
```

In this mode the router advertises the container's own address over mDNS, which nodes cannot reach,
so start every node with the router's URL (below).

The dashboard is at `http://<router>:11435/dashboard`, and clients point at `http://<router>:11435`
as usual.

### Always mount the data volume

`/home/herd/.fleet-manager` holds the trace store, the logs, learned latencies and the install id
that [anonymous usage stats](https://ollamaherd.com/telemetry) use to count your herd once. Without a
named volume there, every recreated container is a new install: your history resets and your herd is
counted again on [ollamaherd.com/stats](https://ollamaherd.com/stats). Herd logs a warning when it
creates a new install id inside a container, so a missing volume shows up in `docker logs`.

For a bind mount instead of a named volume, give the directory to the image's user first:
`sudo chown 10001:10001 /path/to/dir`.

To opt out of usage stats, add `-e FLEET_NODE_TELEMETRY=false` to the router.

## A node on Linux

```bash
docker run -d --name herd-node --network host --restart unless-stopped \
  -v herd-node-data:/home/herd/.fleet-manager \
  ghcr.io/geeks-accelerator/ollama-herd herd-node
```

With the host network the node reaches Ollama at `http://localhost:11434`, whether Ollama runs
natively or in its own container with port 11434 published. Set `FLEET_NODE_OLLAMA_HOST` if it is
elsewhere. The node takes the host's name as its node id; set `FLEET_NODE_NODE_ID` to choose one.

**If the node does not find the router,** skip discovery and give it the URL. This is also the
only option when the router runs in bridge mode or under Docker Desktop:

```bash
docker run -d --name herd-node --network host --restart unless-stopped \
  -e FLEET_NODE_ROUTER_URL=http://192.168.1.10:11435 \
  -v herd-node-data:/home/herd/.fleet-manager \
  ghcr.io/geeks-accelerator/ollama-herd herd-node
```

Nodes contact the router, never the other way round, so the router needs no list of node addresses.

Discovery uses multicast DNS on UDP port 5353. If a firewall blocks it (firewalld zones without
the `mdns` service do; Ubuntu's `ufw` lets it through by default), either open it or use the router
URL. Rootless Docker needs Engine 29.5 or later for `--network host` to use the real host network;
rootless Podman's `--network host` does. A host's own `avahi-daemon` can keep running: they share
the port.

Tested 2026-11-19 on Ubuntu 26.04 (a Lima VM, with the router on another machine): the node
container discovered the router over the network and served requests under rootful Docker 29.10,
rootless Docker 29.10 and rootless Podman 5.7, with `avahi-daemon` running, and it reported the
right address on a host with several interfaces.

**NVIDIA GPUs:** add `--gpus all` (with the NVIDIA Container Toolkit installed) so the node can run
`nvidia-smi`. The dashboard then shows the GPU's name, and from the release after 0.12.8 the node
counts the card's free memory when the router checks whether a model fits; without it the card's
memory is invisible to herd. AMD GPU memory needs nothing extra, since it is read from `/sys`. Either
way herd reads memory only: it does not read GPU load or route by it.

### What a containerised node cannot do

- **Start or restart Ollama.** Outside a container `herd-node` starts Ollama if it is not running;
  in one it stops with *"Ollama binary not found in PATH… Cannot proceed without Ollama."* Start
  Ollama on the host first. The router's "restart Ollama" action does not reach the host either.
- **Run Apple Silicon extras** (MLX, image generation, speech-to-text): those need a Mac, natively.
- **Serve native text embeddings.** The image has the base dependencies only, so embedding models
  are served through Ollama.

## Docker Compose: router and Open WebUI

```yaml
services:
  herd:
    image: ghcr.io/geeks-accelerator/ollama-herd:latest
    ports: ["11435:11435"]
    volumes: ["herd-data:/home/herd/.fleet-manager"]
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11435/fleet/status')"]
      interval: 30s

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports: ["3000:8080"]
    environment:
      OLLAMA_BASE_URL: http://herd:11435
    volumes: ["open-webui:/app/backend/data"]
    depends_on: [herd]
    restart: unless-stopped

volumes:
  herd-data:
  open-webui:
```

Herd speaks the Ollama API, so Open WebUI treats it as one Ollama server and every machine's models
appear in its list. Start each node with `FLEET_NODE_ROUTER_URL=http://<this-host>:11435`, since a
router in bridge mode cannot be discovered.

## Upgrading

```bash
docker pull ghcr.io/geeks-accelerator/ollama-herd:latest
docker rm -f herd && docker run -d ...   # the same command as before, same volume
```

Pin `X.Y.Z` instead of `latest` if you want to choose when to upgrade.

## Building it yourself

```bash
docker build -t ollama-herd .
scripts/docker-smoke.sh ollama-herd   # on Linux with Ollama running and qwen2.5:0.5b pulled
```

The build uses `uv sync --locked`, so it fails rather than resolve dependencies the release was not
tested with.
