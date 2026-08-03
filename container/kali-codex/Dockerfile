FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive

RUN rm -f /etc/apt/sources.list.d/* \
    && printf 'deb http://mirrors.tuna.tsinghua.edu.cn/kali kali-rolling main contrib non-free non-free-firmware\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      file \
      git \
      iproute2 \
      nodejs \
      npm \
      python3 \
      python3-pip \
      tini \
      unzip \
      wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm/_cacache

# Keep the OS toolchain cached separately from the external npm install.
RUN npm config set registry https://registry.npmmirror.com \
    && npm install -g @openai/codex \
    && codex --version \
    && rm -rf /root/.npm/_cacache

WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
