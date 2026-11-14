"""Both agent configs, run as ECS-on-EC2 daemons: application metrics carry the availability zone from the ECS task
metadata endpoint, and nothing else from it.

Each config boots once (see agent_run) against a stub task metadata endpoint, receives two application gauges (one
whose resource names no availability zone, one that names its own) and one trace, and every test asserts on what that
run captured.
"""
import json
import os
import tempfile
from dataclasses import dataclass

import pytest

from conftest import (Compose, FakeEcsTaskMetadata, FakeXRay, Sink, TraceBuilder, send_gauge, send_traces,
                      wait_until)

UNLABELED_GAUGE = "e2e_gauge_without_availability_zone"
LABELED_GAUGE = "e2e_gauge_with_availability_zone"
APPLICATION_AVAILABILITY_ZONE = "us-west-2a"


@dataclass(frozen=True)
class AgentConfig:
    id: str
    service: str
    traces_reach_gateway: bool


CONFIGS = [
    AgentConfig("default", "agent-direct", traces_reach_gateway=False),
    AgentConfig("agent-gateway", "agent-gateway", traces_reach_gateway=True),
]


@dataclass(frozen=True)
class AgentRun:
    config: AgentConfig
    gauge_resources: dict
    trace_resource_attribute_keys: set
    xray_segments: list


def resource_of(sink, metric_name):
    return next((rm["resource"] for rm in sink.metrics() for sm in rm.get("scopeMetrics", [])
                 for m in sm.get("metrics", []) if m["name"] == metric_name), None)


def attributes(resource):
    return {a["key"]: next(iter(a["value"].values())) for a in resource.get("attributes", [])}


@pytest.fixture(scope="module", params=CONFIGS, ids=lambda c: c.id)
def agent_run(request):
    config = request.param
    task_metadata = FakeEcsTaskMetadata()
    fake_xray = FakeXRay()
    try:
        with tempfile.TemporaryDirectory(prefix="otel-e2e-") as out:
            os.chmod(out, 0o777)  # the image runs as nonroot
            env = {"E2E_OUT": out, "FAKE_XRAY_PORT": str(fake_xray.port),
                   "ECS_CONTAINER_METADATA_URI_V4": task_metadata.uri_from_container}
            with Compose("agent-ecs.yaml", env) as compose:
                compose.up(config.service)
                port = compose.port(config.service, 55681)
                send_gauge(port, UNLABELED_GAUGE, 1.0, "foo-service")
                send_gauge(port, LABELED_GAUGE, 2.0, "bar-service",
                           resource_attrs={"cloud.availability_zone": APPLICATION_AVAILABILITY_ZONE})
                trace = TraceBuilder(seed=40).trace(spans=1)
                send_traces(port, [trace])
                sink, gateway = Sink(out, "sink"), Sink(out, "gw")
                wait_until(lambda: all(resource_of(sink, name) for name in (UNLABELED_GAUGE, LABELED_GAUGE)),
                           timeout_s=30)
                if config.traces_reach_gateway:
                    wait_until(lambda: trace.trace_id in gateway.spans_by_trace(), timeout_s=30)
                else:
                    wait_until(lambda: trace.trace_id in fake_xray.segments_by_trace(), timeout_s=30)
                yield AgentRun(
                    config,
                    {name: resource_of(sink, name) for name in (UNLABELED_GAUGE, LABELED_GAUGE)},
                    {key for rs in gateway.resource_spans() for key in attributes(rs.get("resource", {}))},
                    fake_xray.segments_by_trace().get(trace.trace_id, []))
    finally:
        fake_xray.close()
        task_metadata.close()


def test_application_metrics_carry_the_task_availability_zone(agent_run):
    resource = attributes(agent_run.gauge_resources[UNLABELED_GAUGE])
    assert resource.get("cloud.availability_zone") == FakeEcsTaskMetadata.AVAILABILITY_ZONE


def test_task_metadata_adds_no_other_resource_attributes(agent_run):
    """Every other attribute the ECS detector knows (task, cluster, account, region, launch type, log groups) is turned
    off, and the stub serves the fields each one comes from, so re-enabling any of them fails here."""
    resource = attributes(agent_run.gauge_resources[UNLABELED_GAUGE])
    assert set(resource) == {"service.name", "deployment.environment.name", "cloud.availability_zone"}


def test_an_availability_zone_the_application_set_is_kept(agent_run):
    resource = attributes(agent_run.gauge_resources[LABELED_GAUGE])
    assert resource.get("cloud.availability_zone") == APPLICATION_AVAILABILITY_ZONE


def test_traces_do_not_get_the_task_availability_zone(agent_run):
    if agent_run.config.traces_reach_gateway:
        assert agent_run.trace_resource_attribute_keys, "no trace resource reached the gateway stand-in"
        assert "cloud.availability_zone" not in agent_run.trace_resource_attribute_keys
    else:
        assert agent_run.xray_segments, "no segment reached the fake X-Ray"
        assert FakeEcsTaskMetadata.AVAILABILITY_ZONE not in json.dumps(agent_run.xray_segments)
