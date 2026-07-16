# adw sandbox image — runs agents and gates inside an Apple container.
# Build with: adw sandbox build   (wraps `container build`)
#
# Auth is via env secrets forwarded at run time (adw passes `-e ANTHROPIC_API_KEY`
# etc.); nothing interactive. In -p mode Claude Code uses ANTHROPIC_API_KEY, or
# set CLAUDE_CODE_OAUTH_TOKEN to bill against a subscription.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (used by many projects' gate commands).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Agent CLIs. Add or remove to match the backends your adw.yaml uses.
RUN npm install -g @anthropic-ai/claude-code opencode-ai

# Trust the bind-mounted workspace regardless of host uid.
RUN git config --global --add safe.directory '*'

WORKDIR /work
