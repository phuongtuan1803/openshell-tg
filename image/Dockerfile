# Agent image for openshell-tg sandboxes. Versions change only with an
# openshell-tg release, so every sandbox of one release behaves the same.
FROM ubuntu:24.04

# UTF-8 so tmux and the agents handle accented text (Vietnamese) correctly.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

ARG NODE_MAJOR=22
ARG CODEX_VERSION=0.162.1
ARG CLAUDE_CODE_VERSION=2.1.300
ARG OPENCODE_VERSION=1.19.0

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git gh tmux python3 python3-venv python3-pip build-essential less nano \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g --omit=dev \
      @openai/codex@${CODEX_VERSION} \
      @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
      opencode-ai@${OPENCODE_VERSION} \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

COPY otg-agent /usr/local/bin/otg-agent
COPY AGENTS.md /usr/share/openshell-tg/AGENTS.md
RUN chmod 0755 /usr/local/bin/otg-agent

# tmux: Ctrl-B D leaves the agent running; mouse scrolling on; true colour; no key delay.
RUN printf 'set -g mouse on\nset -g history-limit 50000\nset -sg escape-time 10\nset -ga terminal-overrides ",xterm-256color:Tc"\n' > /etc/tmux.conf
