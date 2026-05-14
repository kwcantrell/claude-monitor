FROM node:20

ARG TZ
ENV TZ="$TZ"

ARG CLAUDE_CODE_VERSION=latest

# Install basic development tools
RUN apt-get update && apt-get install -y --no-install-recommends \
  less \
  git \
  procps \
  fzf \
  man-db \
  unzip \
  gnupg2 \
  gh \
  jq \
  nano \
  vim \
  python3 \
  python3-pip \
  ca-certificates \
  python3-dev \
  locales \
  && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
  && locale-gen \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

# uv — fast Python package manager
COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uvx /usr/local/bin/uvx

# Ensure default node user has access to /usr/local/share
RUN mkdir -p /usr/local/share/npm-global && \
  chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist bash history.
RUN mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R $USERNAME /commandhistory

# Set `DEVCONTAINER` environment variable to help with orientation
ENV DEVCONTAINER=true

# Create monitor and config directories and set permissions
RUN mkdir -p /monitor /home/node/.claude \
  /home/node/.cache/uv \
  /home/node/.local/share \
  /home/node/.npm && \
  chown -R node:node /monitor /home/node/.claude \
  /home/node/.cache /home/node/.local/share \
  /home/node/.npm

WORKDIR /monitor

ARG GIT_DELTA_VERSION=0.18.2
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Set up non-root user
USER node

# Install global packages
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin
ENV NODE_PATH=/usr/local/share/npm-global/lib/node_modules

# Set the default shell to bash
ENV SHELL=/bin/bash

# Set the default editor and visual
ENV EDITOR=nano
ENV VISUAL=nano

# Install oh-my-bash
RUN bash -c "$(curl -fsSL https://raw.githubusercontent.com/ohmybash/oh-my-bash/master/tools/install.sh)" --unattended && \
  echo '[ -f /usr/share/doc/fzf/examples/key-bindings.bash ] && source /usr/share/doc/fzf/examples/key-bindings.bash' >> ~/.bashrc && \
  echo '[ -f /usr/share/doc/fzf/examples/completion.bash ] && source /usr/share/doc/fzf/examples/completion.bash' >> ~/.bashrc && \
  echo 'alias python="python3"' >> ~/.bashrc && \
  echo 'export HISTFILE=/commandhistory/.bash_history' >> ~/.bashrc && \
  echo 'export PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }history -a"' >> ~/.bashrc


# Install Claude
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
