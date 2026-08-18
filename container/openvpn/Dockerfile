FROM alpine:3.22

RUN apk add --no-cache ca-certificates iproute2 openvpn

ENTRYPOINT ["/usr/sbin/openvpn"]
