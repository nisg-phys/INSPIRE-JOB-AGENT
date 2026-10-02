import json
import logging

from app.logging_config import _JsonFormatter


def format_line(level, **extra):
    record = logging.LogRecord("app.test", level, __file__, 1, "something_happened", None, None)
    record.__dict__.update(extra)
    return json.loads(_JsonFormatter().format(record))


def test_each_line_carries_the_severity_cloud_logging_reads():
    """Without "severity" Cloud Logging stores every line as DEFAULT, so
    filtering or colouring logs by level shows nothing.
    """
    assert format_line(logging.WARNING)["severity"] == "WARNING"
    assert format_line(logging.ERROR)["severity"] == "ERROR"


def test_an_extra_field_cannot_overwrite_the_severity():
    assert format_line(logging.ERROR, severity="DEBUG")["severity"] == "ERROR"
