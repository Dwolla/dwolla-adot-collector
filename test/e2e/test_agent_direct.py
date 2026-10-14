"""The default agent config (/etc/otel-config.yaml): what it forwards to X-Ray."""
import time

from conftest import Compose, TraceBuilder, send_traces, wait_until

BATCH_TIMEOUT_S = 5


def segment_count(fake_xray):
    return sum(len(docs) for docs in fake_xray.segments_by_trace().values())


def run_direct(fake_xray, traces, env=None, send=None):
    """Runs the default config, sends the traces, and returns the X-Ray segments that arrived, by trace ID."""
    with Compose("agent-direct.yaml", {"FAKE_XRAY_PORT": str(fake_xray.port), **(env or {})}) as compose:
        compose.up()
        (send or (lambda c: send_traces(c.port("agent-a", 55681), traces)))(compose)
        wait_until(lambda: segment_count(fake_xray) > 0, timeout_s=30)
        # Settled once a full batch timeout passes with no new segments.
        previous = -1
        while previous != segment_count(fake_xray):
            previous = segment_count(fake_xray)
            time.sleep(BATCH_TIMEOUT_S + 1)
    expected = {t.trace_id for t in traces}
    return {k: v for k, v in fake_xray.segments_by_trace().items() if k in expected}


def test_forwards_every_span_with_the_environment_annotation(fake_xray):
    builder = TraceBuilder(seed=1)
    traces = [builder.trace(spans=3) for _ in range(5)]
    kept = run_direct(fake_xray, traces, env={"INTERIM_TRACE_SAMPLING_PERCENT": "100"})
    assert set(kept) == {t.trace_id for t in traces}
    for docs in kept.values():
        assert len(docs) == 3
        assert all(d.get("annotations", {}).get("deployment.environment.name") == "Test" for d in docs)
