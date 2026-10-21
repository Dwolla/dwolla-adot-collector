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

The collector also publishes its own metrics on a localhost-only Prometheus endpoint (127.0.0.1:18888) and scrapes them
every 60 s into the same export path (a pull rather than a push, so stopping the collector exits cleanly), e.g.
`otelcol_exporter_sent_spans` and `otelcol_exporter_send_failed_spans` (labeled by `exporter`),
`otelcol_receiver_refused_spans`, and `otelcol_process_memory_rss`, with `service.name` `otelcol-custom`. Because
every collector sends these, every collector's IAM role needs `cloudwatch:PutMetricData`, even where no application
sends metrics; without it, the collector logs a failed metrics export every minute. The scrape doesn't carry start
times for cumulative metrics and CloudWatch requires them, so the `metricstarttime` processor sets each one to the
collector's start time, which is exact because the scraped process is the collector.

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

The self-metrics pieces (the Prometheus pull receiver, the self processors, the `metrics/self` pipeline, and
`service.telemetry.metrics`) live in [`self-metrics/`](self-metrics) and are baked into the image at
`/etc/self-metrics/`. All four configs include them with `${file:...}` references; `otel-config.yaml` is the
example.

### Environment variables

| Variable | Used by | Controls |
|----------|---------|----------|
| `AWS_REGION` | All | The region of the AWS endpoints and the SigV4 signing |
| `DWOLLA_ENV` | All | The `deployment.environment.name` attribute |
| `INTERIM_TRACE_SAMPLING_PERCENT` | Agents | The percentage of traces the agent keeps. Defaults to 40; set it to 100 to turn the interim sampler off |
| `TAIL_SAMPLING_SPANS_PER_SEC` | Gateways | The span budget per second shared by the slices; error traces are kept outside it |
| `TAIL_SAMPLING_NEW_TRACES_PER_SEC` | Gateways | The expected rate of new traces per second; the tail sampler uses it only as a sizing hint |
| `TAIL_SAMPLING_API_PERCENT` | Gateways | The share of the API service's traces kept from its slice of the budget |
| `TAIL_SAMPLING_BATCH_PERCENT` | Gateways | The share of batch-service traces kept from the batch slice |
| `TAIL_SAMPLING_BASELINE_PERCENT` | Gateways | The share of all other traces kept from the baseline slice |
| `TAIL_SAMPLING_BATCH_SERVICES_REGEX` | Gateways | The services treated as batch services |

Gateways must set every `TAIL_SAMPLING_*` variable except the regex. An unset `TAIL_SAMPLING_SPANS_PER_SEC` resolves
to 0, so the gateway keeps only error traces. An unset `*_PERCENT` keeps nothing from its own slice.

`TAIL_SAMPLING_BATCH_SERVICES_REGEX` defaults to matching no service. Anchor the value, as in `\A(svc-a|svc-b)\z`,
because the collector matches it unanchored.

Leave `INTERIM_TRACE_SAMPLING_PERCENT` and `TAIL_SAMPLING_BATCH_SERVICES_REGEX` unset, or set a real value; never set
them empty. The collector applies a `${env:NAME:-default}` default only when `NAME` is absent. An empty
`INTERIM_TRACE_SAMPLING_PERCENT=` becomes 0, so the agent drops every trace. An empty
`TAIL_SAMPLING_BATCH_SERVICES_REGEX=` becomes an empty regex that matches every service, so the baseline slice keeps
nothing.

### What the gateway keeps

Traces over 1000 spans are dropped first, errors included. Every other trace that reaches the gateway with an error span
is kept, uncapped and outside the slice budgets. Every remaining trace is checked against the slices in this order. The
first slice whose rule keeps the trace decides: for the api, batch, and baseline slices that rule includes the
percentage draw, so a trace that loses the draw moves on to the next slice. If the deciding slice is over its budget,
the trace is dropped, not passed on:

| Slice | Matches | Share of `TAIL_SAMPLING_SPANS_PER_SEC` |
|-------|---------|-----------------------------------------|
| slow | whole-trace duration of at least 5000 ms | 19% |
| flagged | a span with the boolean attribute `com.dwolla.keep=true` | 9% |
| api | `service.name` exactly `api` (a literal, not a regex), then kept at `TAIL_SAMPLING_API_PERCENT` | 14% |
| batch-structural | a batch service whose root span has `com.dwolla.sampling.tier` = `structural`; kept whole | 5% |
| batch | any other batch-service trace (`com.dwolla.sampling.tier` = `chunk`), kept at `TAIL_SAMPLING_BATCH_PERCENT` | 32% |
| baseline | every non-batch service, kept at `TAIL_SAMPLING_BASELINE_PERCENT` | 21% |

Each slice's span budget is its share times `TAIL_SAMPLING_SPANS_PER_SEC`, per second, per gateway instance. Slow and
flagged traces are budgeted like the rest. A trace bigger than its slice's budget is never kept by that slice, and it is
not passed on to a later one. At a cap of 600, for example, the flagged slice (9%) holds traces of up to 54 spans and
batch-structural (5%) up to 30. Because error traces are uncapped, an outage can raise trace-backend ingestion, so watch
backend ingestion volume. On the awsxray fallback gateway, a burst can also hit X-Ray's segment-rate quota.

### Interim sampling on the agents

Agents keep a fixed percentage of whole traces with the `probabilistic_sampler` processor in `hash_seed` mode, so every
collector with the same `hash_seed` and percentage keeps or drops the same traces.

- In `hash_seed` mode the interim sampler drops any span whose tracestate already carries an OpenTelemetry `rv` or `th`
  value (`fail_closed` defaults to true). Nothing in a typical setup writes those, but a service that adopts
  OpenTelemetry's consistent-probability samplers would lose its spans at the agent.
- In `hash_seed` mode the sampler honors a span's `sampling.priority` attribute over the trace-ID hash, so a span that
  sets it can be kept or dropped independently of its trace, which produces partial traces.
- Every collector a trace passes through must use the same `hash_seed` and percentage, or traces get split. Never
  change `hash_seed`. Changing `INTERIM_TRACE_SAMPLING_PERCENT` splits traces for as long as old and new collectors
  both run, so change it in one window everywhere the traces flow.
- The interim sampler is temporary. Once every agent and sidecar a trace passes through routes to gateways, set
  `INTERIM_TRACE_SAMPLING_PERCENT=100` everywhere, in one window.

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
