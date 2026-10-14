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

## Configurations

The image ships four configs. The default is the agent config; choose another by overriding the command, e.g.
`--config /etc/otel-gateway.yaml`.

| Path | Role | Traces go to |
|------|------|--------------|
| `/etc/otel-config.yaml` (default) | Agent | X-Ray, through the `awsxray` exporter |
| `/etc/otel-agent-gateway.yaml` | Agent | The gateways, load-balanced by trace ID across the addresses `gateway.otel.internal` resolves to, on port 4317 |
| `/etc/otel-gateway.yaml` | Gateway | The X-Ray OTLP endpoint (CloudWatch Transaction Search), after tail sampling |
| `/etc/otel-gateway-xray.yaml` | Gateway | X-Ray, through the `awsxray` exporter, after tail sampling; use it only when Transaction Search is off, because the OTLP endpoint rejects traces then |

Both gateway configs embed the same tail-sampling policy from `/etc/tail-sampling.yaml`. All four send metrics to
CloudWatch as described above.

### Environment variables

| Variable | Used by | Controls |
|----------|---------|----------|
| `AWS_REGION` | All | The region of the AWS endpoints and the SigV4 signing |
| `DWOLLA_ENV` | All | The `deployment.environment.name` attribute |
| `INTERIM_TRACE_SAMPLING_PERCENT` | Agents | The percentage of traces the agent keeps. Defaults to 40; set it to 100 to turn the interim sampler off |
| `TAIL_SAMPLING_SPANS_PER_SEC` | Gateways | The total span budget per second across all kept traces |
| `TAIL_SAMPLING_NEW_TRACES_PER_SEC` | Gateways | The expected rate of new traces per second; the tail sampler uses it only as a sizing hint |
| `TAIL_SAMPLING_API_PERCENT` | Gateways | The share of the API service's traces kept from its slice of the budget |
| `TAIL_SAMPLING_BATCH_PERCENT` | Gateways | The share of batch-service traces kept from the batch slice |
| `TAIL_SAMPLING_BASELINE_PERCENT` | Gateways | The share of all other traces kept from the baseline slice |
| `TAIL_SAMPLING_BATCH_SERVICES_REGEX` | Gateways | The services treated as batch services |

Gateways must set every `TAIL_SAMPLING_*` variable except the regex. An unset `TAIL_SAMPLING_SPANS_PER_SEC` resolves
to 0, and the gateway then keeps nothing. An unset `*_PERCENT` keeps nothing from its own slice only; errors, slow
traces, and flagged traces are still kept.

`TAIL_SAMPLING_BATCH_SERVICES_REGEX` defaults to matching no service. Anchor the value, as in `\A(svc-a|svc-b)\z`,
because the collector matches it unanchored.

### Interim sampling on the agents

Agents keep a fixed percentage of whole traces with the `probabilistic_sampler` processor in `hash_seed` mode, so every
collector with the same `hash_seed` and percentage keeps or drops the same traces.

- In `hash_seed` mode the interim sampler drops any span whose tracestate already carries an OpenTelemetry `rv` or `th`
  value (`fail_closed` defaults to true). Nothing in a typical setup writes those, but a service that adopts
  OpenTelemetry's consistent-probability samplers would lose its spans at the agent.
- Every collector a trace passes through must use the same `hash_seed` and percentage, or traces get split. Never
  change `hash_seed`. Changing `INTERIM_TRACE_SAMPLING_PERCENT` splits traces for as long as old and new collectors
  both run, so change it in one window everywhere the traces flow.

### Recording which policy kept a span

Gateways can record, on each kept span, which tail-sampling policy kept it. Start the collector with the Alpha feature
gate `--feature-gates=processor.tailsamplingprocessor.recordpolicy`. The decision attributes
(`tailsampling.policy` and `tailsampling.composite_policy`) appear on the instrumentation-scope attributes of the kept
spans.

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

To build the image, validate every config, and run the end-to-end tests under Docker Compose (Docker and Python 3 are
required):

```bash
make check
```
