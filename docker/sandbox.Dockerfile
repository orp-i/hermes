FROM python:3.12-slim

# No-proxy build defaults — Tsinghua TUNA mirror for pip + apt.
# Override: --build-arg PIP_INDEX_URL=... / APT_MIRROR=... / USE_TUNA_MIRROR=0
ARG USE_TUNA_MIRROR=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    DEBIAN_FRONTEND=noninteractive

RUN if [ "${USE_TUNA_MIRROR}" = "1" ]; then \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
      fi; \
      if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
      fi; \
    fi

# System tools + Tier B bioinformatics CLI toolchain
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
      less \
      procps \
      ripgrep \
      tini \
      tree \
      unzip \
      vim-tiny \
      wget \
      xz-utils \
      zip \
      build-essential \
      pkg-config \
      libffi-dev \
      libssl-dev \
      zlib1g-dev \
      libbz2-dev \
      liblzma-dev \
      libcurl4-openssl-dev \
      samtools \
      bcftools \
      bwa \
      bowtie2 \
      minimap2 \
      bedtools \
      tabix && \
    rm -rf /var/lib/apt/lists/*

# Install seqkit (not in Debian repos) - Go binary
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) seqkit_arch="linux_amd64" ;; \
      arm64) seqkit_arch="linux_arm64" ;; \
      *) echo "unsupported arch $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/shenwei356/seqkit/releases/download/v2.8.2/seqkit_${seqkit_arch}.tar.gz" \
      | tar -xz -C /usr/local/bin seqkit; \
    chmod +x /usr/local/bin/seqkit

# Python: scientific stack + bioinformatics + document parsing
RUN pip install --no-cache-dir \
      numpy \
      pandas \
      scipy \
      matplotlib \
      seaborn \
      scikit-learn \
      statsmodels \
      requests \
      httpx \
      beautifulsoup4 \
      lxml \
      openpyxl \
      pyarrow \
      tqdm \
      jupyterlab-server \
      ipykernel \
      biopython \
      pysam \
      pyfaidx \
      pyvcf3 \
      scikit-bio \
      pybedtools \
      gffutils \
      mygene \
      # Document parsing for user-uploaded files
      python-docx \
      python-pptx \
      odfpy \
      striprtf \
      xlrd \
      pypdf \
      chardet \
      markdown

RUN mkdir -p /workspace /code

WORKDIR /workspace

# Container runs as root. Isolation comes from namespaces + mem_limit/pids_limit
# in compose, not from in-container UID. Keeps named-volume permissions simple.
# Long-lived: just sleep. The agent talks to this container via `docker exec`.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sleep", "infinity"]
