"""The default agent config's own metrics: the names and shapes CloudWatch queries depend on, and a clean shutdown.
Regenerate the contract fixture with E2E_WRITE_CONTRACT=1 pytest test/e2e/test_self_metrics.py -k contract."""
import json
import os
from pathlib import Path

import pytest

from conftest import Compose, Sink, TraceBuilder, send_traces, wait_until

CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "self-metrics-contract.json"
EXPORT_INTERVAL_S = 60
STABLE_RESOURCE_ATTRIBUTES = ("service.name", "deployment.environment.name")
DATA_TYPES = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


def _keys(attributes):
    return sorted(a["key"] for a in attributes)


def _string_value(attributes, key):
    return next((a["value"].get("stringValue") for a in attributes if a["key"] == key), None)


def self_metric_contract(resource_metrics):
    """Reduces the sink's OTLP JSON to what must stay stable: each otelcol_ metric's name, data type, monotonicity,
    temporality, unit, and data-point attribute keys, plus the resource attribute keys and the values of the
    stable ones, and the sorted names of all other metrics (none expected: a pull reader's own series would show here).
    Only the metrics present after one export cycle following a small burst of traces are included,
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


def writing_contract():
    return bool(os.environ.get("E2E_WRITE_CONTRACT"))


def expected_self_metric_names():
    """Every self-metric the fixture expects, so the wait covers the whole set and a batch-timing split can't drop
    one. When regenerating, the fixture may be stale, so only the span-flow pair is required."""
    if writing_contract():
        return {"otelcol_receiver_accepted_spans", "otelcol_exporter_sent_spans"}
    return set(json.loads(CONTRACT_FIXTURE.read_text())["metrics"])


def has_self_metrics(sink, expected):
    names = {m["name"] for rm in sink.metrics() for sm in rm.get("scopeMetrics", []) for m in sm.get("metrics", [])}
    return expected <= names


def run_agent_until_exported(e2e_out, fake_xray):
    """Starts the default agent with a sink, sends it a few traces, and returns once the span-flow self-metrics
    have reached the sink."""
    compose = Compose("agent-self-metrics.yaml", {"E2E_OUT": str(e2e_out), "FAKE_XRAY_PORT": str(fake_xray.port)})
    try:
        compose.up()
        builder = TraceBuilder(seed=1)
        send_traces(compose.port("agent", 55681), [builder.trace(spans=3) for _ in range(5)])
        sink = Sink(e2e_out, "sink")
        expected = expected_self_metric_names()
        wait_until(lambda: has_self_metrics(sink, expected), timeout_s=EXPORT_INTERVAL_S + 30)
    except BaseException:
        compose.down()
        raise
    return compose, sink


def test_self_metrics_keep_their_contract(e2e_out, fake_xray):
    compose, sink = run_agent_until_exported(e2e_out, fake_xray)
    with compose:
        contract = self_metric_contract(sink.metrics())
    if writing_contract():
        CONTRACT_FIXTURE.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
        pytest.skip("wrote fixture; rerun without E2E_WRITE_CONTRACT to verify")
    assert contract == json.loads(CONTRACT_FIXTURE.read_text())


def test_default_config_exits_cleanly_when_stopped(e2e_out, fake_xray):
    compose, _ = run_agent_until_exported(e2e_out, fake_xray)
    with compose:
        compose.stop("agent", timeout_s=30)
        code = compose.exit_code("agent")
        tail = "\n".join(compose.logs("agent").splitlines()[-20:])
        assert code == 0, f"agent exited with {code}; last log lines:\n{tail}"
