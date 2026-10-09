FROM python:3.12-slim

# No-proxy build defaults: use the Tsinghua TUNA mirror for pip + apt so the
# build doesn't depend on outbound traffic to pypi.org / deb.debian.org from
# environments that cannot reach them directly. Override at build time with
# `--build-arg PIP_INDEX_URL=...` / `--build-arg APT_MIRROR=...`, or pass
# `--build-arg USE_TUNA_MIRROR=0` to disable both.
ARG USE_TUNA_MIRROR=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    HERMES_HOME=/opt/data

RUN if [ "${USE_TUNA_MIRROR}" = "1" ]; then \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
      fi; \
      if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
      fi; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      jq \
      ripgrep \
      tini && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -u 10000 -m -d /opt/data hermes

WORKDIR /srv/hermes-access
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hermes_config.py ./
COPY app ./app
COPY agent ./agent
COPY llm_router ./llm_router
COPY skills ./skills
COPY --chmod=0755 docker/hermes-wrapper-entrypoint.sh /usr/local/bin/hermes-wrapper-entrypoint

VOLUME ["/opt/data", "/data", "/workspace"]
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/hermes-wrapper-entrypoint"]
