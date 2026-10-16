"""Unit tests for the e2e harness itself; no Docker."""
from conftest import Sink


def sink_lines(tmp_path, content):
    sink = Sink(tmp_path, "sink")
    sink.metrics_file.write_text(content)
    return Sink._lines(sink.metrics_file)


def test_lines_ignores_a_line_the_exporter_is_still_writing(tmp_path):
    assert sink_lines(tmp_path, '{"a": 1}\n{"b":') == [{"a": 1}]


def test_lines_reads_every_complete_line(tmp_path):
    assert sink_lines(tmp_path, '{"a": 1}\n{"b": 2}\n') == [{"a": 1}, {"b": 2}]


def test_lines_of_an_empty_file_is_empty(tmp_path):
    assert sink_lines(tmp_path, "") == []
