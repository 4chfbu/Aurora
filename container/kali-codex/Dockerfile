FROM kalilinux/kali-rolling AS core

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG AURORA_TOOL_MANIFEST_SHA=unknown

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/aurora-venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN rm -f /etc/apt/sources.list.d/* \
    && printf 'deb http://ftp.halifax.rwth-aachen.de/kali kali-rolling main contrib non-free non-free-firmware\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=20 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=20 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends \
      7zip \
      bash \
      binutils \
      binwalk \
      build-essential \
      ca-certificates \
      checksec \
      curl \
      default-jre-headless \
      dirb \
      dmg2img \
      ffuf \
      file \
      foremost \
      gdb \
      gdbserver \
      git \
      gobuster \
      hashid \
      imagemagick \
      iproute2 \
      john \
      jq \
      libfsapfs-utils \
      libimage-exiftool-perl \
      ltrace \
      netcat-openbsd \
      nikto \
      nmap \
      nodejs \
      npm \
      openssl \
      patchelf \
      pngcheck \
      poppler-utils \
      python3 \
      python3-capstone \
      python3-pip \
      python3-pwntools \
      python3-pycryptodome \
      python3-rzpipe \
      python3-sympy \
      python3-unicorn \
      python3-venv \
      python3-z3 \
      qpdf \
      radare2 \
      ripgrep \
      rizin \
      ropper \
      sleuthkit \
      socat \
      sqlmap \
      steghide \
      strace \
      tcpdump \
      tesseract-ocr \
      testdisk \
      tini \
      tshark \
      unrar \
      unzip \
      wafw00f \
      wget \
      whatweb \
      wordlists \
      xxd \
      xz-utils \
      yara \
      zstd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm/_cacache

COPY container/kali-codex/requirements-core.txt /tmp/requirements-core.txt
RUN python3 -m venv --system-site-packages /opt/aurora-venv \
    && pip install --no-cache-dir -r /tmp/requirements-core.txt \
    && rm /tmp/requirements-core.txt

# Keep the OS toolchain cached separately from the external npm install.
RUN npm config set registry https://registry.npmmirror.com \
    && npm install -g @openai/codex \
    && codex --version \
    && rm -rf /root/.npm/_cacache
COPY container/kali-codex/mcp /opt/aurora-mcp
COPY --chmod=0755 container/kali-codex/python-wrapper.sh /usr/local/bin/python
COPY container/kali-codex/codex-config.toml /root/.codex/config.toml
COPY aurora/tool_profiles.json /opt/aurora/tool_profiles.json

LABEL io.aurora.worker.profile="core" \
      io.aurora.tool-manifest-sha256="${AURORA_TOOL_MANIFEST_SHA}"

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]


FROM core AS heavy

ARG AURORA_TOOL_MANIFEST_SHA=unknown
ENV RUSTICL_ENABLE=llvmpipe

RUN apt-get -o Acquire::Retries=20 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=20 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends \
      apktool \
      default-jdk-headless \
      ghidra \
      hashcat \
      jadx \
      khronos-opencl-icd-loader-bin \
      mesa-opencl-icd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY container/kali-codex/requirements-heavy.txt /tmp/requirements-heavy.txt
RUN pip install --no-cache-dir -r /tmp/requirements-heavy.txt \
    && rm /tmp/requirements-heavy.txt
COPY --chmod=0755 container/kali-codex/analyze-headless-wrapper.sh /usr/local/bin/analyzeHeadless
COPY --chmod=0755 container/kali-codex/ghidra-wrapper.sh /usr/local/bin/ghidra
COPY --chmod=0755 container/kali-codex/vol-wrapper.sh /usr/local/bin/vol

LABEL io.aurora.worker.profile="heavy" \
      io.aurora.tool-manifest-sha256="${AURORA_TOOL_MANIFEST_SHA}"
