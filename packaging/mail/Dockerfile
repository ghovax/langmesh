# Headless LangMesh: daemon plus IMAP/SMTP client. Bind nothing but loopback.
FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /srv/langmesh
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY .agents ./.agents
COPY packaging ./packaging
COPY documentation ./documentation
RUN uv sync --no-dev \
    && mkdir -p /srv/langmesh/xdg/config/langmesh \
        /srv/langmesh/xdg/data/langmesh \
        /srv/langmesh/xdg/state/langmesh \
        /srv/langmesh/xdg/cache/langmesh \
        /srv/langmesh/xdg/runtime/langmesh

ENV XDG_CONFIG_HOME=/srv/langmesh/xdg/config \
    XDG_DATA_HOME=/srv/langmesh/xdg/data \
    XDG_STATE_HOME=/srv/langmesh/xdg/state \
    XDG_CACHE_HOME=/srv/langmesh/xdg/cache \
    XDG_RUNTIME_DIR=/srv/langmesh/xdg/runtime

COPY packaging/mail/docker-entrypoint.sh /usr/local/bin/langmesh-mail-entrypoint
RUN chmod +x /usr/local/bin/langmesh-mail-entrypoint
VOLUME ["/srv/langmesh/xdg"]
ENTRYPOINT ["/usr/local/bin/langmesh-mail-entrypoint"]
