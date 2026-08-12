# {{PACKAGE_NAME}} image — adds the domain toolchain to a base Hermes agent image.
#
# Build:
#   docker build -f docker/Dockerfile \
#     -t {{IMAGE_NAME}}:<tag> \
#     --build-arg BASE_IMAGE=ghcr.io/cyborg-garden/hermes-agent-mt:<sha> .
#
# DESIGN — three load-bearing decisions (each corresponds to a real failure mode
# against the hermes-agent-mt base; do not undo them):
#
#   1. NO ENTRYPOINT override. The base runs s6-overlay: its real entrypoint is
#      ["/init", "/opt/hermes/docker/main-wrapper.sh"]. /init is PID 1 — it
#      supervises services and drops to the unprivileged user internally.
#      Overriding ENTRYPOINT bypasses s6 entirely (no supervision, no priv-drop).
#
#   2. NO USER directive. s6's /init must start as root to set up the service
#      tree and then drop privileges itself. Pinning USER in the Dockerfile stops
#      s6 from initializing. Privilege drop is s6's job, not Docker's.
#
#   3. NO code COPY. The package, plugin, and skill are provided at runtime via
#      the mounted /opt/data volume. Baking them into the image would create drift
#      against the hot-mounted checkout and make updates require a full rebuild.
#      This image is exactly the system + domain binaries the agent shells out to;
#      everything else lives in the (git-pull-updatable) data mount. Plugin
#      enablement and skill wiring are handled by instance-setup.sh, not here.
#
# Net effect: a thin, s6-clean layer that turns any hermes-agent-mt image into
# one whose shell has your domain toolchain on PATH.

ARG BASE_IMAGE=ghcr.io/cyborg-garden/hermes-agent-mt:latest
FROM ${BASE_IMAGE}

USER root

# ── System binaries (apt) ─────────────────────────────────────────────
# Add the system-level tools your domain requires. Keep this list minimal —
# include only binaries the agent shells out to, not Python libraries that
# should be installed via pip below. Remove this block if your domain has
# no system-level dependencies.
#
# {{PLACEHOLDER: Replace or extend the package list below. Examples by domain:
#   OSINT/recon:    whois dnsutils nmap tor torsocks mediainfo
#   Journalism:     pandoc poppler-utils tesseract-ocr
#   Health/bio:     samtools bcftools
#   Data/ETL:       freetds-dev libpq-dev
#   Remove the pip block entirely if you have no Python CLI tools.}}
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq \
        curl \
        # {{PLACEHOLDER: add domain apt packages here}} \
    && rm -rf /var/lib/apt/lists/*

# ── Python domain tools ───────────────────────────────────────────────
# Install against the SYSTEM python (/usr/bin/python3), not `python3` — the
# app venv is first on PATH and has no pip. --break-system-packages lands the
# console scripts in /usr/local/bin, available to the agent's shell-outs.
#
# Remove this block entirely if your package has no Python CLI dependencies.
#
# {{PLACEHOLDER: Replace with your domain's pip packages.}}
# RUN /usr/bin/python3 -m pip install --no-cache-dir --break-system-packages \
#         {{TOOL_1}} \
#         {{TOOL_2}}

# Inherit the base s6-overlay ENTRYPOINT and the root USER context set above —
# intentionally left as-is. See design note (2) above.
