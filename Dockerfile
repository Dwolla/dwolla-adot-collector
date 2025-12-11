# Use Go 1.24 to satisfy builder v0.141.0 toolchain requirement
FROM golang:1.24-alpine AS builder

# Install build dependencies
RUN apk --update add ca-certificates git

# Install the builder tool v0.141.0
RUN go install go.opentelemetry.io/collector/cmd/builder@v0.141.0

WORKDIR /build

# Copy the config AND the local source code
COPY builder-config.yaml .
COPY healthchecker.go .
COPY pkg ./pkg  

# Generate the source code and build the binary
RUN builder --config builder-config.yaml

# Build the healthcheck utility
RUN go build -o /build/dist/healthcheck healthchecker.go

# Stage 2: Final Image
FROM gcr.io/distroless/static:latest
LABEL maintainer="Dwolla Dev <dev+dwolla-adot-collector@dwolla.com>"
LABEL org.label-schema.vcs-url="https://github.com/Dwolla/dwolla-adot-collector"

# Switch to non-root user for security
USER nonroot:nonroot

COPY --from=builder --chown=nonroot:nonroot /build/dist/otelcol-custom /otelcol-custom
COPY --from=builder --chown=nonroot:nonroot /build/dist/healthcheck /healthcheck

# Expose standard OTLP ports
EXPOSE 4317 4318 13133

COPY --chown=nonroot:nonroot otel-config.yaml /etc/otel-config.yaml

HEALTHCHECK \
  --interval=30s \
  --timeout=5s \
  --retries=3 \
  --start-period=60s \
  --start-interval=5s \
  CMD ["/healthcheck"]

ENTRYPOINT ["/otelcol-custom"]
CMD ["--config", "/etc/otel-config.yaml"]
