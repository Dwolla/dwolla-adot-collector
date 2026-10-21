"""Every config's own metrics: the names and shapes CloudWatch queries depend on, and a clean shutdown.

Each of the four configs (default agent, gateway-routing agent, gateway, and X-Ray fallback gateway) gets every test,
with its own compose file and traffic. A config's stack boots once (see collector_run) and every test for that config
asserts on what that one run captured. The gateways reach tail-sampling decisions; the gateway-routing agent
load-balances traces to two stand-in gateways.

The contract fixture is the contract of the old push path, where the collector pushed its own metrics over OTLP to its
own receiver. The pull path (metrics/self) must reproduce it. Regenerating the fixture from this config would only
record the pull path's own output and prove nothing, so regenerate it from an image built with a push-path config.
The three gateway configs were push-path through 0369abf, so to regenerate a gateway fixture, check out 0369abf in
step 1 and use the same commands:

1. git clone . /tmp/push-ref && git -C /tmp/push-ref checkout b03703f
   (the last commit with the push-path config; after a collector upgrade, check out the upgrade instead and set
   otel-config.yaml back to the push path: the periodic OTLP reader to localhost:4317, without prometheus/self and
   metrics/self)
2. docker buildx build --load --tag dwolla/otel-collector:push-ref /tmp/push-ref
3. From this checkout (the config comes from the image):
   OTELCOL_IMAGE=dwolla/otel-collector:push-ref E2E_WRITE_CONTRACT=1 target/venv/bin/pytest test/e2e/test_self_metrics.py -k contract
   (add -k "contract and <config id>" to write one config's fixture)
4. docker image rm dwolla/otel-collector:push-ref && rm -rf /tmp/push-ref, then run make check against this config.
"""
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from conftest import Compose, FakeXRay, Sink, TraceBuilder, _attr, send_traces, wait_until

FIXTURES = Path(__file__).parent / "fixtures"
EXPORT_INTERVAL_S = 60
# A gateway's tail sampler holds a trace for its decision wait before the first decision metrics exist.
DECISION_WAIT_S = 30
# 30 traces of 3 spans in one request: more than batch/traces' send_batch_size (50) at once, so a batch is sent by size
TRACE_COUNT = 30
SECOND_CYCLE_METRIC = "otelcol_exporter_sent_metric_points"
APPLICATION_GAUGE = "e2e_application_gauge"
STABLE_RESOURCE_ATTRIBUTES = ("service.name", "deployment.environment.name", "com.dwolla.otel.collector.role")
DATA_TYPES = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


@dataclass(frozen=True)
class Config:
    """One collector config under test: how to start it, which service is the collector, where its OTLP/HTTP receiver
    listens, and what only it emits (so a fixture regeneration waits for those too)."""
    id: str
    compose: str
    service: str
    otlp_port: int
    fixture: Path
    error_traces: bool = False
    wait_extra_s: int = 0
    generation_required: frozenset = frozenset()


SPAN_FLOW = frozenset({
    "otelcol_receiver_accepted_spans",
    "otelcol_exporter_sent_spans",
})
AGENT_GATEWAY_ONLY = frozenset({
    "otelcol_loadbalancer_num_backends",
    "otelcol_loadbalancer_backend_outcome",
    "otelcol_processor_probabilistic_sampler_count_traces_sampled",
})
GATEWAY_ONLY = frozenset({
    "otelcol_processor_tail_sampling_count_traces_sampled",
})
CONFIGS = [
    Config("default", "agent-self-metrics.yaml", "agent", 55681, FIXTURES / "self-metrics-contract.json",
           generation_required=SPAN_FLOW | {"otelcol_processor_batch_batch_size_trigger_send"}),
    Config("agent-gateway", "agent-gateway-self-metrics.yaml", "agent", 55681,
           FIXTURES / "self-metrics-contract-agent-gateway.json",
           generation_required=SPAN_FLOW | AGENT_GATEWAY_ONLY),
    Config("gateway", "gateway-self-metrics.yaml", "gateway", 4318, FIXTURES / "self-metrics-contract-gateway.json",
           error_traces=True, wait_extra_s=DECISION_WAIT_S, generation_required=SPAN_FLOW | GATEWAY_ONLY),
    Config("gateway-xray", "gateway-xray-self-metrics.yaml", "gateway", 4318,
           FIXTURES / "self-metrics-contract-gateway-xray.json",
           error_traces=True, wait_extra_s=DECISION_WAIT_S, generation_required=SPAN_FLOW | GATEWAY_ONLY),
]

def _keys(attributes):
    return sorted(a["key"] for a in attributes)


def _string_value(attributes, key):
    return next((a["value"].get("stringValue") for a in attributes if a["key"] == key), None)


def self_metric_contract(resource_metrics):
    """Reduces the sink's OTLP JSON to what must stay stable: each otelcol_ metric's name, data type, monotonicity,
    temporality, unit, and data-point attribute keys, plus the resource attribute keys and the values of the
    stable ones, and the sorted names of all other metrics (none expected: a pull reader's own series would show here).
    Only the metrics present after two export cycles following a burst of traces are included,
    and nothing that varies per run (instance IDs, timestamps, values)."""
    metrics = {}
    other_metric_names = set()
    resource_keys = set()
    resource_values = {}
    for rm in resource_metrics:
        attributes = rm.get("resource", {}).get("attributes", [])
        resource_keys.update(_keys(attributes))
        for key in STABLE_RESOURCE_ATTRIBUTES:
            value = _string_value(attributes, key)
            if value is not None:
                resource_values[key] = value
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                if not metric["name"].startswith("otelcol_"):
                    other_metric_names.add(metric["name"])
                    continue
                data_type = next(t for t in DATA_TYPES if t in metric)
                body = metric[data_type]
                entry = metrics.setdefault(metric["name"], {
                    "type": data_type,
                    "isMonotonic": body.get("isMonotonic"),
                    "aggregationTemporality": body.get("aggregationTemporality"),
                    "unit": metric.get("unit"),
                    "dataPointAttributeKeys": [],
                })
                keys = set(entry["dataPointAttributeKeys"])
                for point in body.get("dataPoints", []):
                    keys.update(_keys(point.get("attributes", [])))
                entry["dataPointAttributeKeys"] = sorted(keys)
    return {
        "metrics": dict(sorted(metrics.items())),
        "otherMetricNames": sorted(other_metric_names),
        "resourceAttributeKeys": sorted(resource_keys),
        "resourceAttributeValues": dict(sorted(resource_values.items())),
    }


def self_metric_instance_ids(resource_metrics):
    """The service.instance.id values on the resources that carry otelcol_ metrics."""
    return {
        _string_value(rm.get("resource", {}).get("attributes", []), "service.instance.id")
        for rm in resource_metrics
        if any(m["name"].startswith("otelcol_") for sm in rm.get("scopeMetrics", []) for m in sm.get("metrics", []))}


def collector_instance_ids(logs):
    """The service.instance.id every collector log line carries: the collector's own UUID."""
    return set(re.findall(r'"service\.instance\.id": "([^"]+)"', logs))


def writing_contract():
    return bool(os.environ.get("E2E_WRITE_CONTRACT"))


def expected_self_metric_names(config):
    """Every self-metric the fixture expects, so the wait covers the whole set and a batch-timing split can't drop
    one. When regenerating, the fixture may be stale, so only the config's required set is waited for: the span-flow
    pair, what only this config emits, and a metric that first appears in the second export cycle (it counts the first
    cycle's export)."""
    if writing_contract():
        return {*config.generation_required, SECOND_CYCLE_METRIC}
    return set(json.loads(config.fixture.read_text())["metrics"])


def has_self_metrics(sink, expected):
    names = {m["name"] for rm in sink.metrics() for sm in rm.get("scopeMetrics", []) for m in sm.get("metrics", [])}
    return expected <= names


@dataclass(frozen=True)
class CollectorRun:
    """What one config's collector run captured, read by every test for that config."""
    config: Config
    resource_metrics: list
    logs_while_running: str
    application_gauge_resource: dict
    exit_code: object
    logs_after_stop: str


def application_gauge_reached_sink(sink):
    return any(m["name"] == APPLICATION_GAUGE
               for rm in sink.metrics() for sm in rm.get("scopeMetrics", []) for m in sm.get("metrics", []))


def drive_collector(config, compose, sink):
    """Starts the config, sends it a burst of traces, waits until every expected self-metric has reached the sink
    (two export cycles), and snapshots the sink and logs. Only then sends the application gauge, so it can't show up in
    the self-metric snapshot, waits for it, and stops the collector to record its exit code."""
    compose.up()
    builder = TraceBuilder(seed=1)
    port = compose.port(config.service, config.otlp_port)
    send_traces(port, [builder.trace(spans=3, error=config.error_traces and i % 5 == 0) for i in range(TRACE_COUNT)])
    expected = expected_self_metric_names(config)
    wait_until(lambda: has_self_metrics(sink, expected), timeout_s=2 * EXPORT_INTERVAL_S + 30 + config.wait_extra_s)
    resource_metrics = sink.metrics()
    logs_while_running = compose.logs(config.service)
    send_gauge(port, APPLICATION_GAUGE, 42.0, "foo-service")
    wait_until(lambda: application_gauge_reached_sink(sink), timeout_s=30)
    gauge_resource = next(rm["resource"] for rm in sink.metrics() for sm in rm.get("scopeMetrics", [])
                          for m in sm.get("metrics", []) if m["name"] == APPLICATION_GAUGE)
    compose.stop(config.service, timeout_s=30)
    return CollectorRun(config, resource_metrics, logs_while_running, gauge_resource,
                        compose.exit_code(config.service), compose.logs(config.service))


@pytest.fixture(scope="module", params=CONFIGS, ids=lambda c: c.id)
def collector_run(request):
    """One collector run per config, shared by every test in this module for that config. A failure raises the same
    error, naming the config, in each test that depends on the run."""
    config = request.param
    fake_xray = FakeXRay()
    try:
        with tempfile.TemporaryDirectory(prefix="otel-e2e-") as out:
            os.chmod(out, 0o777)  # the image runs as nonroot
            with Compose(config.compose, {"E2E_OUT": out, "FAKE_XRAY_PORT": str(fake_xray.port)}) as compose:
                try:
                    run = drive_collector(config, compose, Sink(out, "sink"))
                except Exception as e:
                    raise RuntimeError(f"the shared collector run for config '{config.id}' failed: {e}") from e
                yield run
    finally:
        fake_xray.close()


def test_self_metrics_keep_their_contract(collector_run):
    config = collector_run.config
    resource_metrics = collector_run.resource_metrics
    collector_ids = collector_instance_ids(collector_run.logs_while_running)
    assert len(collector_ids) == 1, f"expected one collector instance ID in the logs, got {collector_ids}"
    # Each collector's self-metrics must keep its own ID; a shared one would merge every collector into one series.
    assert self_metric_instance_ids(resource_metrics) == collector_ids
    contract = self_metric_contract(resource_metrics)
    if writing_contract():
        config.fixture.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
        pytest.skip("wrote fixture; rerun without E2E_WRITE_CONTRACT to verify")
    assert contract == json.loads(config.fixture.read_text())


def test_self_metrics_all_have_units(collector_run):
    contract = self_metric_contract(collector_run.resource_metrics)
    without_unit = sorted(name for name, metric in contract["metrics"].items() if not metric["unit"])
    assert not without_unit, f"self-metrics reached the sink without a unit; extend transform/self-metrics: {without_unit}"


CUMULATIVE_TEMPORALITIES = ("AGGREGATION_TEMPORALITY_CUMULATIVE", 2)


def cumulative_self_metric_points(resource_metrics):
    """Every cumulative Sum and Histogram data point on an otelcol_ metric, as (series, metric name, point), where the
    series is the metric name, the data-point attributes, and the collector's service.instance.id."""
    for rm in resource_metrics:
        instance_id = _string_value(rm.get("resource", {}).get("attributes", []), "service.instance.id")
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                if not metric["name"].startswith("otelcol_"):
                    continue
                for data_type in ("sum", "histogram"):
                    body = metric.get(data_type)
                    if body is None or body.get("aggregationTemporality") not in CUMULATIVE_TEMPORALITIES:
                        continue
                    for point in body.get("dataPoints", []):
                        attributes = json.dumps(sorted(point.get("attributes", []), key=lambda a: a["key"]), sort_keys=True)
                        yield (metric["name"], attributes, instance_id), metric["name"], point


def test_cumulative_self_metrics_carry_stable_start_times(collector_run):
    """The backend rejects a cumulative point without a start time, and a start time that moves between exports reads
    as a reset on every export."""
    points = list(cumulative_self_metric_points(collector_run.resource_metrics))
    assert points, "no cumulative otelcol_ data points reached the sink"
    invalid = sorted({
        (name, point.get("startTimeUnixNano"), point["timeUnixNano"])
        for _, name, point in points
        if not (int(point.get("startTimeUnixNano", 0)) > 0
                and int(point["startTimeUnixNano"]) < int(point["timeUnixNano"]))})
    assert not invalid, "cumulative self-metric points need 0 < start < end; (name, start, end): " + "; ".join(
        f"{name} start={start} end={end}" for name, start, end in invalid)
    start_times = {}
    for series, _, point in points:
        start_times.setdefault(series, set()).add(point["startTimeUnixNano"])
    assert any(
        len({point["timeUnixNano"] for s, _, point in points if s == series}) >= 2 for series in start_times
    ), "no cumulative series was exported twice, so stability can't be checked"
    moving = sorted((series[0], sorted(times)) for series, times in start_times.items() if len(times) > 1)
    assert not moving, f"start times changed between exports; (name, start times): {moving}"


def test_exits_cleanly_when_stopped(collector_run):
    service = collector_run.config.service
    tail = "\n".join(collector_run.logs_after_stop.splitlines()[-20:])
    assert collector_run.exit_code == 0, f"{service} exited with {collector_run.exit_code}; last log lines:\n{tail}"


def send_gauge(port, name, value, service):
    payload = {"resourceMetrics": [{
        "resource": {"attributes": [_attr("service.name", service)]},
        "scopeMetrics": [{"scope": {"name": "e2e"}, "metrics": [{
            "name": name,
            "gauge": {"dataPoints": [{"asDouble": value, "timeUnixNano": str(time.time_ns())}]},
        }]}],
    }]}
    request = Request(f"http://127.0.0.1:{port}/v1/metrics", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        assert response.status == 200, response.status


def test_application_metrics_still_flow(collector_run):
    attributes = collector_run.application_gauge_resource["attributes"]
    assert _string_value(attributes, "service.name") == "foo-service"
    assert _string_value(attributes, "deployment.environment.name") == "Test"
