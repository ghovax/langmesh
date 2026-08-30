FROM nixos/nix:2.35.2

WORKDIR /app

ENV NIX_CONFIG="experimental-features = nix-command flakes"
ENV PATH="/root/.local/state/nix/profiles/profile/bin:/root/.nix-profile/bin:${PATH}"
ENV LD_LIBRARY_PATH="/root/.local/state/nix/profiles/profile/lib:/root/.nix-profile/lib:${LD_LIBRARY_PATH}"
ENV UV_LINK_MODE=copy

# Keep the operational toolchain in the image. The GitHub session can add more
# packages to its private profile at runtime without modifying this image.
COPY flake.nix flake.lock pyproject.toml uv.lock README.md ./
COPY nix ./nix
RUN nix profile install --accept-flake-config --priority 4 \
    nixpkgs#autoconf \
    nixpkgs#automake \
    nixpkgs#bun \
    nixpkgs#cacert \
    nixpkgs#clang \
    nixpkgs#clang-tools \
    nixpkgs#cmake \
    nixpkgs#coreutils \
    nixpkgs#curl \
    nixpkgs#diffutils \
    nixpkgs#fd \
    nixpkgs#file \
    nixpkgs#findutils \
    nixpkgs#gawk \
    nixpkgs#gh \
    nixpkgs#git \
    nixpkgs#gnumake \
    nixpkgs#gnugrep \
    nixpkgs#gnused \
    nixpkgs#gnutar \
    nixpkgs#gzip \
    nixpkgs#jq \
    nixpkgs#less \
    nixpkgs#libtool \
    nixpkgs#llvm \
    nixpkgs#meson \
    nixpkgs#ninja \
    nixpkgs#nodejs_22 \
    nixpkgs#openssl \
    nixpkgs#pkg-config \
    nixpkgs#python313 \
    nixpkgs#ripgrep \
    nixpkgs#ruff \
    nixpkgs#rustc \
    nixpkgs#stdenv.cc.cc.lib \
    nixpkgs#unzip \
    nixpkgs#uv \
    nixpkgs#which \
    nixpkgs#zip \
    .#render-cli
RUN nix profile install --accept-flake-config --priority 3 nixpkgs#procps
RUN nix profile install --accept-flake-config --priority 3 nixpkgs#gcc

COPY . .
RUN uv sync --frozen --no-dev
RUN cd web && bun install --frozen-lockfile && bun run build

EXPOSE 10000

CMD ["uv", "run", "--no-dev", "langmesh", "github", "--configuration", "/etc/secrets/github.yaml", "--host", "0.0.0.0", "--port", "10000"]
