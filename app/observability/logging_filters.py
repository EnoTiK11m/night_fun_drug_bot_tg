"""Bound repeated operational diagnostics without hiding distinct root causes."""
from collections import OrderedDict
import hashlib
import logging
import threading
import time

from app.observability.errors import error_details
from app.observability.logic_trace import sanitize


class RepeatedDiagnosticFilter(logging.Filter):
    def __init__(self, *, clock=time.monotonic, interval=300, capacity=512):
        super().__init__()
        self.clock, self.interval, self.capacity = clock, interval, capacity
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def filter(self, record):
        if hasattr(record, '_diagnostic_allowed'):
            return record._diagnostic_allowed
        if record.levelno < logging.WARNING:
            return True
        # Exception roots are stable despite outer messages. Plain warnings use their
        # format template and safe arguments; distinct posts/causes remain distinct.
        evidence = sanitize(error_details(record.exc_info[1])) if record.exc_info else sanitize(record.getMessage())
        key = hashlib.sha256(repr((record.name, record.funcName, evidence)).encode()).hexdigest()
        with self.lock:
            now = self.clock()
            entry = self.entries.get(key)
            if entry is None:
                self.entries[key] = [now, 1]
                if len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
                allowed = True
            else:
                entry[1] += 1
                allowed = now-entry[0] >= self.interval
                if allowed:
                    message = record.getMessage()
                    record.msg = 'Repeated diagnostic count=%s: %s'
                    record.args = (entry[1], message if not record.exc_info else evidence.get('root_error_type', 'unknown'))
                    record.exc_info = None
                    record.exc_text = None
                    entry[0] = now
            record._diagnostic_allowed = allowed
            return allowed


repeated_diagnostic_filter = RepeatedDiagnosticFilter()
