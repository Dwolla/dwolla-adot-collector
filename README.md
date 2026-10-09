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

## Metrics

The collector exports OTLP metrics to the [Amazon CloudWatch OTLP metrics endpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)
in the region named by the `AWS_REGION` environment variable, signed with SigV4. To export metrics:

- set `AWS_REGION` and `DWOLLA_ENV` in the collector's environment. `DWOLLA_ENV` becomes the
  `deployment.environment.name` resource attribute, unless the sending service already set one
- grant the collector's IAM role `cloudwatch:PutMetricData`

Metrics are queryable with PromQL in CloudWatch Query Studio.

The collector also exports its own metrics once a minute through the same pipeline, e.g.
`otelcol_exporter_sent_spans` and `otelcol_exporter_send_failed_spans` (labeled by `exporter`),
`otelcol_receiver_refused_spans`, and `otelcol_process_memory_rss`, with `service.name` `otelcol-custom`. Because
every collector sends these, every collector's IAM role needs `cloudwatch:PutMetricData`, even where no application
sends metrics; without it, the collector logs a failed metrics export every minute.

## Versioning

- Image tags are this repository's release version (e.g. `dwolla/otel-collector:v0.47.0`). Images are pushed only when a `v*` release tag is created; builds of branches and pull requests are built but not pushed.
- Up to v0.45.1, the version matched the upstream AWS Distro for OpenTelemetry (ADOT) collector release the image was based on, and images were tagged `<upstream version>-<commit>` (e.g. `v0.45.1-82a61a1`). Since v0.46.0 the image builds its own collector distribution, so its version is independent of upstream.
- The upstream OpenTelemetry Collector component version is recorded in the `com.dwolla.otel-collector.upstream-version` image label, alongside the standard `org.opencontainers.image.version` label.

## Local Development

To build this image locally:

```bash
make all
```

For multi-architecture builds:

```bash
make PLATFORM=linux/arm64,linux/amd64 OUTPUT=--push all
```
