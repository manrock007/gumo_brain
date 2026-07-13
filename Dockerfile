# gumo_brain — Sentry webhook -> headless Claude Code -> draft PR
# Built for linux/arm64 (Graviton host), works on amd64 too.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates ripgrep \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends gh nodejs \
    && rm -rf /var/lib/apt/lists/*

# ClickUp progress-comment CLI used by Claude during fixes
COPY bin/brain-ticket /usr/local/bin/brain-ticket
RUN chmod +x /usr/local/bin/brain-ticket

RUN useradd --create-home --uid 1000 brain \
    && mkdir /data && chown brain:brain /data
USER brain
WORKDIR /home/brain

# Claude Code CLI (native installer, per-user)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/brain/.local/bin:${PATH}"

COPY --chown=brain:brain requirements.txt /srv/gumo_brain/requirements.txt
RUN pip install --user --no-cache-dir -r /srv/gumo_brain/requirements.txt

COPY --chown=brain:brain app /srv/gumo_brain/app
COPY --chown=brain:brain entrypoint.sh /srv/gumo_brain/entrypoint.sh

WORKDIR /srv/gumo_brain
EXPOSE 8010
ENTRYPOINT ["./entrypoint.sh"]
