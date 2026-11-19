# One image for both roles: `herd` (the router, the default command) and
# `herd-node`. Built from uv.lock, so it carries exactly the dependency set the
# release was tested and CVE-audited against, not whatever PyPI resolves today.
#
# Where it fits (docs/guides/docker.md): the router anywhere, a Linux node only
# with --network host. Never a Mac or Windows node: Docker there is a Linux VM,
# and the node would report the VM's memory and cores instead of the machine's.
#
# Debian slim, never Alpine: the NVIDIA Container Toolkit mounts a glibc
# nvidia-smi into the container under --gpus, which is how a node names its GPU.

ARG PYTHON_VERSION=3.14

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1
WORKDIR /app
# Dependencies first, in their own layer, so a source change does not
# reinstall them. --locked fails the build if uv.lock is stale.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:${PYTHON_VERSION}-slim-trixie
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/geeks-accelerator/ollama-herd" \
      org.opencontainers.image.description="Ollama Herd: one local AI endpoint for every machine you own" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
# A fixed UID, so a bind-mounted data directory can be chowned to it.
RUN groupadd --system --gid 10001 herd \
 && useradd --system --uid 10001 --gid herd --create-home --home-dir /home/herd herd
COPY --from=build /app/.venv /app/.venv
# The data directory holds the telemetry install ID, the trace store and logs.
# Created here, owned by herd, so a new named volume starts with that owner:
# without a volume every recreated container is a new herd on ollamaherd.com/stats.
RUN mkdir -p /home/herd/.fleet-manager && chown herd:herd /home/herd/.fleet-manager
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NVIDIA_DRIVER_CAPABILITIES=utility
USER herd
WORKDIR /home/herd
VOLUME /home/herd/.fleet-manager
EXPOSE 11435
CMD ["herd"]
