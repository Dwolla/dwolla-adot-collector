"""The fallback gateway config (/etc/otel-gateway-xray.yaml): same decisions, exported through awsxray."""
import time

from conftest import Compose, TraceBuilder, send_traces

DECISION_WAIT_S = 30


def run(fake_xray, traces):
    with Compose("gateway-xray.yaml", {"FAKE_XRAY_PORT": str(fake_xray.port)}) as c:
        c.up()
        send_traces(c.port("gateway", 4318), traces)
        time.sleep(DECISION_WAIT_S + 12)
    return fake_xray.segments_by_trace()


def test_keeps_errors_drops_plain_and_extracts_links(fake_xray):
    b = TraceBuilder(seed=30)
    target = b.trace(error=True)
    linking = b.trace(error=True, link_to=target)
    plain = b.trace()
    kept = run(fake_xray, [target, linking, plain])
    assert target.trace_id in kept and linking.trace_id in kept
    assert plain.trace_id not in kept
    root = next(d for d in kept[linking.trace_id] if "parent_id" not in d)
    assert target.trace_id[8:] in str(root.get("annotations", {}).get("linked_trace_ids", ""))


def test_never_sends_more_than_50_segments_per_request(fake_xray):
    big = TraceBuilder(seed=31).trace(spans=120, error=True)
    kept = run(fake_xray, [big])
    assert len(kept[big.trace_id]) == 120
    assert max(len(r) for r in fake_xray.requests) <= 50
