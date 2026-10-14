"""The gateway config (/etc/otel-gateway.yaml): tail-sampling decisions, exported over OTLP."""
import json
import time

from conftest import Compose, Sink, TraceBuilder, send_traces, wait_until

DECISION_WAIT_S = 30
BATCH = "foo-batch"
BASE_ENV = {
    "TAIL_SAMPLING_SPANS_PER_SEC": "600",
    "TAIL_SAMPLING_API_PERCENT": "100",
    "TAIL_SAMPLING_NEW_TRACES_PER_SEC": "100",
    "TAIL_SAMPLING_BATCH_PERCENT": "0",
    "TAIL_SAMPLING_BATCH_SERVICES_REGEX": rf"\A({BATCH}|bar-batch)\z",
}


def attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}


def composite_policies(sink):
    """The composite sub-policy that kept each trace; the processor records it on the scope, not the span."""
    kept_by = {}
    for line in sink.traces_file.read_text().splitlines():
        for rs in json.loads(line).get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                policy = attrs(ss.get("scope", {})).get("tailsampling.composite_policy")
                for span in ss["spans"]:
                    kept_by[span["traceId"]] = policy
    return kept_by


def run_gateway(e2e_out, traces, **env):
    with Compose("gateway.yaml", {"E2E_OUT": str(e2e_out), **BASE_ENV, **env}) as c:
        c.up()
        send_traces(c.port("gateway", 4318), traces)
        time.sleep(DECISION_WAIT_S + 12)
        return Sink(e2e_out, "sink")


def crafted(seed):
    b = TraceBuilder(seed)
    return {
        "error": b.trace(error=True),
        "slow": b.trace(duration_ms=6000),
        "flagged": b.trace(root_attrs={"com.dwolla.keep": True}),
        "plain": b.trace(),
        "structural": b.trace(service=BATCH, root_attrs={"com.dwolla.sampling.tier": "structural"}),
        "chunk": b.trace(service=BATCH, root_attrs={"com.dwolla.sampling.tier": "chunk"}),
        "oversized": b.trace(spans=1001, error=True),
    }


def test_decisions_with_baseline_off(e2e_out):
    t = crafted(seed=20)
    # With a 10000 cap the errors slice (2100 spans) fits the 1001-span error trace, so only drop-oversized rejects it.
    sink = run_gateway(e2e_out, list(t.values()), TAIL_SAMPLING_BASELINE_PERCENT="0",
                       TAIL_SAMPLING_SPANS_PER_SEC="10000")
    kept = sink.spans_by_trace()
    for name in ("error", "slow", "flagged", "structural"):
        assert t[name].trace_id in kept, name
        assert len(kept[t[name].trace_id]) == len(t[name].spans), name
    for name in ("plain", "chunk", "oversized"):
        assert t[name].trace_id not in kept, name
    assert composite_policies(sink)[t["error"].trace_id] == "errors"


def test_baseline_keeps_ordinary_traces_but_never_batch_chunks(e2e_out):
    t = crafted(seed=21)
    lookalike = TraceBuilder(seed=210).trace(service=f"{BATCH}-api")  # contains the batch name, isn't a batch service
    sink = run_gateway(e2e_out, [t["plain"], t["chunk"], lookalike], TAIL_SAMPLING_BASELINE_PERCENT="100")
    kept = sink.spans_by_trace()
    assert t["plain"].trace_id in kept
    assert t["chunk"].trace_id not in kept
    assert lookalike.trace_id in kept  # the anchored regex doesn't match substrings


def test_unset_batch_regex_excludes_nothing_from_baseline(e2e_out):
    t = crafted(seed=22)
    sink = run_gateway(e2e_out, [t["plain"], t["chunk"]], TAIL_SAMPLING_BASELINE_PERCENT="100",
                       TAIL_SAMPLING_BATCH_SERVICES_REGEX="")
    kept = sink.spans_by_trace()
    assert t["plain"].trace_id in kept
    assert t["chunk"].trace_id in kept  # with no batch services configured, it's an ordinary trace


def test_gateway_keeps_nothing_without_its_cap(e2e_out):
    t = crafted(seed=23)
    sink = run_gateway(e2e_out, [t["error"], t["plain"]], TAIL_SAMPLING_BASELINE_PERCENT="100",
                       TAIL_SAMPLING_SPANS_PER_SEC="")
    assert sink.spans_by_trace() == {}  # an unset cap resolves to 0, which fails closed


def test_self_metrics_carry_the_gateway_role(e2e_out):
    with Compose("gateway.yaml", {"E2E_OUT": str(e2e_out), **BASE_ENV, "TAIL_SAMPLING_BASELINE_PERCENT": "10"}) as c:
        c.up()

        def role_seen():
            for rm in Sink(e2e_out, "sink").metrics():
                resource = {a["key"]: next(iter(a["value"].values())) for a in rm["resource"].get("attributes", [])}
                if resource.get("com.dwolla.otel.collector.role") == "gateway":
                    return True
            return False

        wait_until(role_seen, timeout_s=30)
