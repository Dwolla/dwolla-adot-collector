# AWS Distribution for OpenTelemetry Collector (as configured by Dwolla)

[![](https://images.microbadger.com/badges/image/dwolla/otel-collector.svg)](https://microbadger.com/images/dwolla/otel-collector)
[![license](https://img.shields.io/github/license/dwolla/dwolla-adot-collector.svg?style=flat-square)](https://github.com/Dwolla/dwolla-adot-collector/blob/master/LICENSE)

Custom OpenTelemetry Collector distribution with AWS components and Dwolla-specific processors.

## Why a Custom Build?

This distribution builds upon the AWS Distro for OpenTelemetry (ADOT) by adding:

1. **Transform Processor** - Enables efficient pattern replacement in span attributes (e.g., replacing `!` with `:` in `peer.service`)
2. **Link Extractor Processor** - Custom processor that extracts linked trace IDs from OpenTelemetry span links and copies them to span attributes

### The Link Extractor Problem

AWS X-Ray does not natively support OpenTelemetry's [span links](https://opentelemetry.io/docs/concepts/signals/traces/#span-links) concept. When traces reference other traces via links, this relationship is lost when exported to X-Ray.

The `linkextractor` processor solves this by:
- Extracting trace IDs from `span.links`
- Copying them to a `linked_trace_ids` attribute on the span
- Making these relationships visible in X-Ray as indexed attributes

This allows Dwolla to maintain trace relationships and query for linked traces in X-Ray.

## Local Development

To build this image locally:

```bash
make all
```

For multi-architecture builds:

```bash
make PLATFORM=linux/arm64,linux/amd64 OUTPUT=--push all
```
