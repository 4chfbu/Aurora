FROM aurora-kali-codex:latest

ARG CC_SWITCH_BINARY_SHA256=975eac469c7b75ae0d8747169824aa863145a209106a93043149cbc4cafe3e29

COPY .runtime-cache/cc-switch /usr/local/bin/cc-switch

RUN echo "${CC_SWITCH_BINARY_SHA256}  /usr/local/bin/cc-switch" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/cc-switch \
    && cc-switch --version \
    && test "$(cc-switch --version)" = "cc-switch 5.9.3"

COPY container/cc-switch/entrypoint.sh /usr/local/bin/aurora-cc-switch

RUN chmod 0755 /usr/local/bin/aurora-cc-switch

ENV CC_SWITCH_CONFIG_DIR=/var/lib/cc-switch \
    HOME=/var/lib/cc-switch/home \
    XDG_RUNTIME_DIR=/run/cc-switch \
    CC_SWITCH_LISTEN_PORT=15723

EXPOSE 15723

ENTRYPOINT ["/usr/local/bin/aurora-cc-switch"]
