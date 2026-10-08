# MyPlow as a Plow cloud agent: 1-click from aiworthusing.com/agent-index/myplow.
#
# The FULL MyPlow image (every browser and coding CLI it ships) plus the three things a Plow VM needs
# and a Mac does not. ~4 GB; cloud/Dockerfile.slim is the same MyPlow at ~0.9 GB without the Playwright
# browsers and the codex/grok CLIs (Claude agents only, no in-container browsing):
#   - its OWNER's Claude subscription login, fetched at boot from the owner's login server
#     (claude-login.sh). PROTOTYPE: every copy runs on that one person's Claude plan, and the server
#     hands the login only to agents on the owner's Plow account -- anyone else's install boots
#     without one and texts its installer that it only works for its owner. No login is in the image;
#   - the plow-chat plugin switched on, which reads the VM's line from PLOW_API_BASE;
#   - the Agent Index reporter, fed by agentsview, which is the only way it sees Claude Code.
# Nothing tenant-specific is baked in: the token comes from Plow's proxy at run time.
#
# It updates itself in place (cloud/update.py): everything a release changes -- the MyPlow package,
# Claude Code, agentsview, these scripts -- is ONE directory, /opt/myplow/releases/<MYPLOW_RELEASE>,
# and it is the image's LAST layer, so a running VM fetches just that layer from the image Plow pins
# and switches to it on the same disk. entrypoint.sh (PID 1) is the one file an update never replaces.
#
# Published by .github/workflows/publish-container.yml as ghcr.io/delattre1/mypeople:cloud-<version>,
# with BASE and MYPLOW_RELEASE set to that same release. By hand:
#   docker buildx build --platform linux/amd64 -f cloud/Dockerfile \
#     --build-arg BASE=... --build-arg MYPLOW_RELEASE=<version> .
ARG BASE=ghcr.io/delattre1/mypeople:latest

FROM python:3.12-slim AS wheel
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY mypeople ./mypeople
RUN pip wheel --no-deps --wheel-dir /dist .

FROM ${BASE} AS release
USER root
ARG MYPLOW_RELEASE
ARG AGENTSVIEW_VERSION=0.44.0
ARG AGENTSVIEW_SHA256=037ea7a46d52e06b20363b4aa7cd7f28e32f31d8215803d6e9a0c96bac5818e3
ARG CLIENT_REV=edf196031803e204cdbcd81ce574e1f54fd75f65
ARG CLIENT_SHA256=970caf7534cd7d3b71ffee8f1a576f9da4dc494a508e8ab1998ee2ce6f4a2ac4
COPY --from=wheel /dist /dist
COPY cloud/run.sh cloud/update.py cloud/agent-index.sh cloud/claude-login.sh cloud/own-login.py cloud/claude-settings.json cloud/cloud-tools.md /tmp/rel/
COPY cloud/browser-mcp/package.json cloud/browser-mcp/package-lock.json /tmp/rel/browser-mcp/
RUN set -eux; \
    (cd /tmp/rel/browser-mcp && npm ci --omit=dev --no-audit --no-fund --silent); \
    test -n "${MYPLOW_RELEASE}"; \
    R="/opt/myplow/releases/${MYPLOW_RELEASE}"; mkdir -p "$R/bin" "$R/py"; \
    pip install --no-cache-dir --no-deps --target "$R/py" /dist/mypeople-*.whl; \
    printf '#!/bin/sh\nexec python3 -m mypeople.cli "$@"\n' > "$R/bin/mypeople"; \
    cp -L /usr/local/bin/claude "$R/bin/claude"; \
    curl -fsSL "https://github.com/wesm/agentsview/releases/download/v${AGENTSVIEW_VERSION}/agentsview_${AGENTSVIEW_VERSION}_linux_amd64.tar.gz" -o /tmp/av.tgz; \
    echo "${AGENTSVIEW_SHA256}  /tmp/av.tgz" | sha256sum -c -; \
    tar -xzf /tmp/av.tgz -C "$R/bin" agentsview; \
    curl -fsSL "https://raw.githubusercontent.com/plow-pbc/agent-index-client/${CLIENT_REV}/standalone/agent_index_client.py" \
      -o "$R/agent_index_client.py"; \
    echo "${CLIENT_SHA256}  $R/agent_index_client.py" | sha256sum -c -; \
    mv /tmp/rel/* "$R/"; echo "${MYPLOW_RELEASE}" > "$R/RELEASE"; \
    chmod 0755 "$R/bin/"* "$R/run.sh" "$R/update.py" "$R/agent-index.sh" "$R/claude-login.sh" "$R/own-login.py"; \
    ln -s "releases/${MYPLOW_RELEASE}" /opt/myplow/current

FROM ${BASE}
USER root
ARG MYPLOW_RELEASE
COPY cloud/entrypoint.sh /opt/myplow/entrypoint.sh
# The owner's model choice, for the very first boot; run.sh re-asserts it from the release on every
# boot after that, since an update keeps the home where this file lives. The Boss is spawned without
# --model, so on a Mac it follows the owner's own ~/.claude/settings.json; without one here Claude
# Code fell back to its default (Sonnet).
COPY --chown=mypeople:mypeople cloud/claude-settings.json /home/mypeople/.claude/settings.json
RUN chmod 0755 /opt/myplow/entrypoint.sh && chown mypeople:mypeople /opt/myplow \
 && install -d -o mypeople -g mypeople /opt/myplow/releases /var/lib/mypeople/snapshots

ENV PLOW_CHAT=1 \
    DEFAULT_CLAUDE_MODEL=claude-opus-5-5 \
    DEFAULT_ENG_MODEL=claude-opus-5-5 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Plow's VMs (exe.dev) boot the image's CMD, not its ENTRYPOINT, and may boot it as root: the
# base image's ENTRYPOINT is cleared so `docker run` boots the same CMD, and entrypoint.sh drops
# to the mypeople user itself.
USER mypeople
ENTRYPOINT []
CMD ["/opt/myplow/entrypoint.sh"]
LABEL myplow.release="${MYPLOW_RELEASE}"
# LAST layer, by contract with update.py: the release dir (and the link to it). Nothing that writes
# the filesystem may come after this line.
COPY --from=release --chown=mypeople:mypeople /opt/myplow/ /opt/myplow/
