"""The agent → gateway config (/etc/otel-agent-gateway.yaml)."""
import time

from conftest import Compose, Sink, TraceBuilder, send_traces, wait_until


def env(fake_xray, e2e_out, **extra):
    return {"FAKE_XRAY_PORT": str(fake_xray.port), "E2E_OUT": str(e2e_out), **extra}


def kept_by_gateways(e2e_out, expected_ids, settle_s=8):
    time.sleep(settle_s)
    sinks = [Sink(e2e_out, "gw-1").spans_by_trace(), Sink(e2e_out, "gw-2").spans_by_trace()]
    return [{k: v for k, v in s.items() if k in expected_ids} for s in sinks]


def test_every_span_of_a_trace_reaches_the_same_gateway(fake_xray, e2e_out):
    builder = TraceBuilder(seed=10)
    traces = [builder.trace(spans=5) for _ in range(200)]
    with Compose("agent-gateway.yaml", env(fake_xray, e2e_out, INTERIM_TRACE_SAMPLING_PERCENT="100")) as c:
        c.up()
        port = c.port("agent", 55681)
        for i in range(5):  # one span of every trace per request, so a trace's spans arrive in different batches
            send_traces(port, traces, spans_of=lambda t, i=i: t.spans[i:i + 1])
        gw1, gw2 = kept_by_gateways(e2e_out, {t.trace_id for t in traces})
    assert gw1 and gw2, "both gateways should receive traces"
    assert not set(gw1) & set(gw2), "a trace was split across gateways"
    assert set(gw1) | set(gw2) == {t.trace_id for t in traces}
    assert all(len(spans) == 5 for spans in [*gw1.values(), *gw2.values()])


def test_direct_and_gateway_agents_keep_the_same_traces(fake_xray, e2e_out):
    builder = TraceBuilder(seed=11)
    traces = [builder.trace(spans=2) for _ in range(500)]
    with Compose("agent-gateway.yaml", env(fake_xray, e2e_out)) as c:
        c.up()
        send_traces(c.port("agent", 55681), traces)
        send_traces(c.port("agent-direct", 55681), traces)
        gw1, gw2 = kept_by_gateways(e2e_out, {t.trace_id for t in traces})
    via_gateway = set(gw1) | set(gw2)
    via_direct = set(fake_xray.segments_by_trace()) & {t.trace_id for t in traces}
    assert 150 <= len(via_gateway) <= 250, len(via_gateway)
    assert via_gateway == via_direct


def test_agent_starts_before_any_gateway_exists(fake_xray, e2e_out):
    builder = TraceBuilder(seed=12)
    with Compose("agent-gateway.yaml", env(fake_xray, e2e_out, INTERIM_TRACE_SAMPLING_PERCENT="100")) as c:
        c.up("agent")  # gw-1 and gw-2 aren't running, so gateway.otel.internal doesn't resolve
        time.sleep(10)
        assert c.exit_code("agent") is None, c.logs("agent")
        c.up("gw-1", "gw-2")
        time.sleep(10)  # > resolver interval (5 s)
        traces = [builder.trace(spans=2) for _ in range(20)]
        send_traces(c.port("agent", 55681), traces)
        wait_until(lambda: len(set(Sink(e2e_out, "gw-1").spans_by_trace()) | set(Sink(e2e_out, "gw-2").spans_by_trace())) == 20,
                   timeout_s=30)


def attributes_of(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}


def test_both_agent_configs_transform_spans_identically(fake_xray, e2e_out):
    """The gateway agent applies the same span transforms as the default config. (The default config's output arrives
    as X-Ray segments, a different shape, so this pins the gateway path to the transforms the default config defines.)"""
    trace = TraceBuilder(seed=13).trace(spans=1, root_attrs={
        "client.http.uri": "https://example.com/x", "client.http.method": "GET", "http.status_code": "200",
        "peer.service": "foo!bar"})
    with Compose("agent-gateway.yaml", env(fake_xray, e2e_out, INTERIM_TRACE_SAMPLING_PERCENT="100")) as c:
        c.up()
        send_traces(c.port("agent", 55681), [trace])
        gw1, gw2 = kept_by_gateways(e2e_out, {trace.trace_id})
    (span,) = {**gw1, **gw2}[trace.trace_id]
    attrs = attributes_of(span)
    assert attrs["url.full"] == "https://example.com/x"
    assert attrs["http.request.method"] == "GET"
    status = next(a["value"] for a in span["attributes"] if a["key"] == "http.response.status_code")
    assert status == {"intValue": "200"}  # transform/status-code-is-int; OTLP/JSON writes int64 as a string
    assert attrs["peer.service"] == "foo:bar"
    assert attrs["deployment.environment.name"] == "Test"
    assert not {"client.http.uri", "client.http.method", "http.status_code"} & attrs.keys()
    assert "linked_trace_ids" not in attrs  # link extraction belongs in front of awsxray only
