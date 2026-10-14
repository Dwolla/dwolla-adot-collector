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


def test_default_keeps_about_40_percent_of_traces_and_whole_traces_only(fake_xray):
    builder = TraceBuilder(seed=2)
    traces = [builder.trace(spans=3) for _ in range(1000)]
    kept = run_direct(fake_xray, traces)
    assert 350 <= len(kept) <= 450, len(kept)
    assert all(len(docs) == 3 for docs in kept.values())


def test_percent_100_keeps_every_trace(fake_xray):
    builder = TraceBuilder(seed=3)
    traces = [builder.trace(spans=3) for _ in range(300)]
    kept = run_direct(fake_xray, traces, env={"INTERIM_TRACE_SAMPLING_PERCENT": "100"})
    assert len(kept) == 300


def test_two_agents_agree_on_every_trace(fake_xray):
    """Roots go to one agent and children to another (a trace crossing tiers); each trace is kept whole or not at all."""
    builder = TraceBuilder(seed=4)
    traces = [builder.trace(spans=3) for _ in range(500)]

    def split(compose):
        send_traces(compose.port("agent-a", 55681), traces, spans_of=lambda t: t.spans[:1])
        send_traces(compose.port("agent-b", 55681), traces, spans_of=lambda t: t.spans[1:])

    kept = run_direct(fake_xray, traces, send=split)
    assert 150 <= len(kept) <= 250, len(kept)
    assert all(len(docs) == 3 for docs in kept.values())
