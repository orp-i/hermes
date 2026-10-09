FROM python:3.12-slim

ARG USE_TUNA_MIRROR=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

RUN if [ "${USE_TUNA_MIRROR}" = "1" ]; then \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
      fi; \
      if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
      fi; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /srv/runner
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<1" \
      "uvicorn[standard]>=0.30,<1" \
      "pydantic>=2,<3" \
      "docker>=7,<8"

COPY runner/ ./runner/

EXPOSE 8650
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["uvicorn", "runner.app:app", "--host", "0.0.0.0", "--port", "8650"]
