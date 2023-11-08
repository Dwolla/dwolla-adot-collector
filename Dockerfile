ARG OTEL_TAG
FROM amazon/aws-otel-collector:${OTEL_TAG}
LABEL maintainer="Dwolla Dev <dev+dwolla-adot-collector@dwolla.com>"
LABEL org.label-schema.vcs-url="https://github.com/Dwolla/dwolla-adot-collector"

COPY otel-config.yaml /etc/otel-config.yaml
