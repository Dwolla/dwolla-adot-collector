"""Shared harness for the collector's end-to-end tests: docker compose, a fake X-Ray, a stub ECS task metadata
endpoint, a sink reader, traces, and metrics."""
import json
import os
import random
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

COMPOSE_DIR = Path(__file__).parent / "compose"


def wait_until(predicate, timeout_s, interval_s=1.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(f"condition not met within {timeout_s}s")


class Compose:
    def __init__(self, file, env=None):
        self.file = str(COMPOSE_DIR / file)
        self.project = f"e2e-{uuid.uuid4().hex[:8]}"
        self.env = {k: v for k, v in (env or {}).items() if v != ""}

    def _run(self, *args, check=True):
        result = subprocess.run(["docker", "compose", "-p", self.project, "-f", self.file, *args],
                                env={**os.environ, **self.env}, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"docker compose {' '.join(args)} exited {result.returncode}:\n{result.stderr}")
        return result

    def up(self, *services, wait=True):
        self._run("up", "-d", *(["--wait"] if wait else []), *services)
        return self

    def port(self, service, container_port):
        out = self._run("port", service, str(container_port)).stdout.strip()
        return int(out.rsplit(":", 1)[1])

    def logs(self, service):
        return self._run("logs", "--no-color", service, check=False).stdout

    def exit_code(self, service):
        out = self._run("ps", "-a", "--format", "json", service, check=False).stdout.strip()
        for line in out.splitlines():
            state = json.loads(line)
            if state.get("State") == "exited":
                return state.get("ExitCode")
        return None

    def stop(self, service, timeout_s=30):
        self._run("stop", "-t", str(timeout_s), service)

    def down(self):
        self._run("down", "-v", check=False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.down()


class FakeXRay:
    """Accepts PutTraceSegments (and OTLP metric posts) and records them."""

    def __init__(self):
        self.requests = []
        self.metric_posts = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with fake._lock:
                    if self.path.startswith("/v1/metrics"):
                        fake.metric_posts += 1
                    else:
                        docs = json.loads(body).get("TraceSegmentDocuments", [])
                        fake.requests.append([json.loads(d) for d in docs])
                reply = b"{}" if self.path.startswith("/v1/") else b'{"UnprocessedTraceSegments": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def segments_by_trace(self):
        by_trace = {}
        with self._lock:
            for docs in self.requests:
                for doc in docs:
                    _, epoch, rest = doc["trace_id"].split("-")
                    by_trace.setdefault(epoch + rest, []).append(doc)
        return by_trace

    def close(self):
        self._server.shutdown()


@pytest.fixture
def fake_xray():
    server = FakeXRay()
    yield server
    server.close()


class FakeEcsTaskMetadata:
    """A stand-in for the ECS task metadata endpoint v4, as an ECS-on-EC2 daemon task sees it. A collector given
    ECS_CONTAINER_METADATA_URI_V4=uri_from_container reads the task document from <uri>/task and its own container's
    document from <uri>."""

    ACCOUNT = "111122223333"
    REGION = "us-west-2"
    AVAILABILITY_ZONE = "us-west-2b"
    PATH = "/v4/0123456789abcdef-collector"
    COLLECTOR_DOCKER_ID = "0123456789abcdef-collector"

    def __init__(self):
        task_arn = f"arn:aws:ecs:{self.REGION}:{self.ACCOUNT}:task/foo-cluster/0123456789abcdef0123456789abcdef"
        collector = self._container(self.COLLECTOR_DOCKER_ID, "otel-collector", "otel-collector")
        # A second awslogs container, so the detector would find log groups and streams if their attributes were on.
        log_shipper = self._container("fedcba9876543210-shipper", "log-shipper", "log-shipper")
        task = {
            "Cluster": f"arn:aws:ecs:{self.REGION}:{self.ACCOUNT}:cluster/foo-cluster",
            "TaskARN": task_arn,
            "Family": "otel-collector-daemon",
            "Revision": "7",
            "DesiredStatus": "RUNNING",
            "KnownStatus": "RUNNING",
            "LaunchType": "EC2",
            "AvailabilityZone": self.AVAILABILITY_ZONE,
            "Containers": [collector, log_shipper],
        }
        documents = {self.PATH: collector, f"{self.PATH}/task": task}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                document = documents.get(self.path)
                reply = json.dumps(document).encode() if document is not None else b"{}"
                self.send_response(200 if document is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self._server.server_address[1]
        self.uri_from_container = f"http://host.docker.internal:{self.port}{self.PATH}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _container(self, docker_id, name, log_group):
        return {
            "DockerId": docker_id,
            "Name": name,
            "DockerName": f"ecs-otel-collector-daemon-7-{name}",
            "Image": f"example/{name}:latest",
            "Type": "NORMAL",
            "DesiredStatus": "RUNNING",
            "KnownStatus": "RUNNING",
            "LogDriver": "awslogs",
            "LogOptions": {"awslogs-group": log_group, "awslogs-region": self.REGION,
                           "awslogs-stream": f"{name}/{docker_id}"},
            "ContainerARN": f"arn:aws:ecs:{self.REGION}:{self.ACCOUNT}:container/foo-cluster/{docker_id}",
            "Networks": [{"NetworkMode": "bridge", "IPv4Addresses": ["172.17.0.2"]}],
        }

    def close(self):
        self._server.shutdown()


@pytest.fixture
def e2e_out():
    with tempfile.TemporaryDirectory(prefix="otel-e2e-") as d:
        os.chmod(d, 0o777)  # the image runs as nonroot
        yield Path(d)


class Sink:
    def __init__(self, out_dir, name):
        self.traces_file = Path(out_dir) / f"{name}-traces.jsonl"
        self.metrics_file = Path(out_dir) / f"{name}-metrics.jsonl"

    @staticmethod
    def _lines(path):
        if not path.exists():
            return []
        text = path.read_text()
        # The file exporter may be mid-write: only lines it has finished (terminated by "\n") are complete JSON.
        complete = text[: text.rfind("\n") + 1]
        return [json.loads(line) for line in complete.splitlines() if line.strip()]

    def spans_by_trace(self):
        by_trace = {}
        for request in self._lines(self.traces_file):
            for rs in request.get("resourceSpans", []):
                for ss in rs.get("scopeSpans", []):
                    for span in ss.get("spans", []):
                        by_trace.setdefault(span["traceId"], []).append(span)
        return by_trace

    def resource_spans(self):
        return [rs for request in self._lines(self.traces_file) for rs in request.get("resourceSpans", [])]

    def metrics(self):
        return [rm for request in self._lines(self.metrics_file) for rm in request.get("resourceMetrics", [])]


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


@dataclass
class Trace:
    trace_id: str
    service: str
    spans: list = field(default_factory=list)


class TraceBuilder:
    """Seeded, so every run sends the same trace and span IDs."""

    def __init__(self, seed):
        self.rng = random.Random(seed)

    def _hex(self, n_bytes):
        return "".join(f"{self.rng.randrange(256):02x}" for _ in range(n_bytes))

    def trace(self, service="foo-service", spans=3, error=False, duration_ms=10, root_attrs=None, link_to=None,
              start_ns=None):
        trace_id = self._hex(16)
        start = start_ns or (time.time_ns() - duration_ms * 1_000_000)
        end = start + duration_ms * 1_000_000
        root_id = self._hex(8)
        root = {"traceId": trace_id, "spanId": root_id, "name": "root", "kind": 2,
                "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
                "attributes": [_attr(k, v) for k, v in (root_attrs or {}).items()],
                "status": {"code": 2} if error else {}}
        if link_to is not None:
            root["links"] = [{"traceId": link_to.trace_id, "spanId": link_to.spans[0]["spanId"]}]
        children = [{"traceId": trace_id, "spanId": self._hex(8), "parentSpanId": root_id, "name": f"child-{i}",
                     "kind": 3, "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 1_000_000)}
                    for i in range(spans - 1)]
        return Trace(trace_id, service, [root, *children])


def send_traces(port, traces, spans_of=lambda t: t.spans):
    by_service = {}
    for t in traces:
        by_service.setdefault(t.service, []).extend(spans_of(t))
    payload = {"resourceSpans": [
        {"resource": {"attributes": [_attr("service.name", service)]},
         "scopeSpans": [{"scope": {"name": "e2e"}, "spans": spans}]}
        for service, spans in by_service.items() if spans]}
    if not payload["resourceSpans"]:
        return
    request = Request(f"http://127.0.0.1:{port}/v1/traces", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        assert response.status == 200, response.status


def send_gauge(port, name, value, service, resource_attrs=None):
    payload = {"resourceMetrics": [{
        "resource": {"attributes": [_attr("service.name", service),
                                    *(_attr(k, v) for k, v in (resource_attrs or {}).items())]},
        "scopeMetrics": [{"scope": {"name": "e2e"}, "metrics": [{
            "name": name,
            "gauge": {"dataPoints": [{"asDouble": value, "timeUnixNano": str(time.time_ns())}]},
        }]}],
    }]}
    request = Request(f"http://127.0.0.1:{port}/v1/metrics", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        assert response.status == 200, response.status
