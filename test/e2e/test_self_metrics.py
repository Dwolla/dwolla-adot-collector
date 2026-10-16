"""The default agent config's own metrics: the names and shapes CloudWatch queries depend on, and a clean shutdown."""
import json
import os
from pathlib import Path

from conftest import Compose, Sink, wait_until

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
    stable ones. Only the metrics present after one export cycle with no traffic are included, and nothing that
    varies per run (instance IDs, timestamps, values)."""
    metrics = {}
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
        "resourceAttributeKeys": sorted(resource_keys),
        "resourceAttributeValues": dict(sorted(resource_values.items())),
    }


def has_self_metrics(sink):
    return any(m["name"].startswith("otelcol_")
               for rm in sink.metrics() for sm in rm.get("scopeMetrics", []) for m in sm.get("metrics", []))


def run_agent_until_exported(e2e_out):
    """Starts the default agent with a sink and returns once a self-metric has reached the sink."""
    compose = Compose("agent-self-metrics.yaml", {"E2E_OUT": str(e2e_out)})
    try:
        compose.up()
        sink = Sink(e2e_out, "sink")
        wait_until(lambda: has_self_metrics(sink), timeout_s=EXPORT_INTERVAL_S + 30)
    except BaseException:
        compose.down()
        raise
    return compose, sink


def test_self_metrics_keep_their_contract(e2e_out):
    compose, sink = run_agent_until_exported(e2e_out)
    with compose:
        contract = self_metric_contract(sink.metrics())
    if os.environ.get("E2E_WRITE_CONTRACT"):
        CONTRACT_FIXTURE.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    assert contract == json.loads(CONTRACT_FIXTURE.read_text())


def test_default_config_exits_cleanly_when_stopped(e2e_out):
    compose, _ = run_agent_until_exported(e2e_out)
    with compose:
        compose.stop("agent", timeout_s=30)
        code = compose.exit_code("agent")
        tail = "\n".join(compose.logs("agent").splitlines()[-20:])
        assert code == 0, f"agent exited with {code}; last log lines:\n{tail}"
