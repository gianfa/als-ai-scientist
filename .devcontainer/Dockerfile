# syntax=docker/dockerfile:1
#
# Development / execution environment for ALS AI Scientist.
# Contains only what this repository uses: Python 3.10 + uv, Node 22 (TypeScript contracts and
# workflow tests), Claude Code (run.sh launches the workflows through it), the Paper2Agent skill,
# and LaTeX + pandoc (the technical-report build, report export).
# No secrets and no data are baked into the image. Tool versions are pinned via build args.

ARG PYTHON_VERSION=3.10
ARG NODE_VERSION=22.23.3
ARG UV_VERSION=0.12.19

FROM node:${NODE_VERSION}-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
# 2.1.277+ is compatible with the .agents convention; Dynamic Workflows depend on the CLI version.
ARG CLAUDE_VERSION=2.1.284
ARG PAPER2AGENT_COMMIT=8c2d059165ef8cdcb70dbea76655b9c2b55b38e6
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    PATH=/home/vscode/.local/bin:${PATH}

# System packages: git/curl/CA for tooling, jq for scripts/ops/check-running-workflows.sh,
# LaTeX (latexmk + bibtex, TikZ, fonts, enumitem/ragged2e) and pandoc for the report.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        latexmk \
        pandoc \
        texlive-bibtex-extra \
        texlive-fonts-recommended \
        texlive-latex-extra \
        texlive-latex-recommended \
        texlive-pictures \
    && rm -rf /var/lib/apt/lists/*

# Node.js and uv, copied from pinned official images.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=uv /uv /uvx /usr/local/bin/

# Claude Code (pinned) and the Paper2Agent skill (pinned commit). The skill is kept outside
# ~/.claude because devcontainer.json mounts a volume there; postCreate.sh copies it in.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && git clone https://github.com/jmiao24/Paper2Agent.git /opt/Paper2Agent \
    && git -C /opt/Paper2Agent checkout --detach "${PAPER2AGENT_COMMIT}" \
    && rm -rf /opt/Paper2Agent/.git

# Non-root user matching the host UID/GID (avoids root-owned files in the bind mount).
RUN groupadd --gid "${GID}" vscode \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash vscode \
    && install -d -o vscode -g vscode /home/vscode/.claude /home/vscode/.cache/uv

USER vscode
RUN claude --version && uv --version && node --version && pandoc --version | head -1 && latexmk --version | head -2

WORKDIR /app/als-ai-scientist
CMD ["sleep", "infinity"]
