# The Instagram comment agent: one agent session (Grok or Claude) in tmux, in a box of its own.
# It answers public comments on its own, so anyone commenting can try to steer it. It gets no host
# mounts and nothing but the reply token; the plugin that owns it builds and starts it.
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends tmux curl python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code
# grok publishes no checksum, so the artifact digest is pinned here (same as the main image).
ARG GROK_VERSION=1.0.41
ARG GROK_SHA256=9ce03ed23e16ea01072b4496263d6213a27899e1e3e107f008d36edf82e70407
RUN curl -fsSL "https://x.ai/cli/grok-${GROK_VERSION}-linux-x86_64" -o /usr/local/bin/grok \
    && echo "${GROK_SHA256}  /usr/local/bin/grok" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/grok

USER node
WORKDIR /home/node
COPY --chown=node CLAUDE.md persona.md ./
COPY --chown=node skills /home/node/.claude/skills
COPY --chown=node skills /home/node/.grok/skills
COPY --chown=node entrypoint.sh /home/node/entrypoint.sh
CMD ["bash", "/home/node/entrypoint.sh"]
