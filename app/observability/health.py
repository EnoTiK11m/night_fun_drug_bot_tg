"""Process-local observations, bounded and independent of business state."""
from collections import OrderedDict, deque
from copy import deepcopy
import hashlib
import threading
import time


class RuntimeHealth:
    def __init__(self, *, clock=time.time, summary_seconds=300, capacity=100):
        self.clock, self.summary_seconds, self.capacity = clock, summary_seconds, capacity
        self.lock = threading.RLock()
        self.components = {}
        self.incidents = deque(maxlen=capacity)
        self.active = OrderedDict()
        self.sequence = 0
        self.process = {"started_at": clock(), "stage": "constructed"}

    def error(self, component, operation, details, request_id=None, attempt=None):
        now = self.clock()
        fingerprint = hashlib.sha256(repr((component, operation, details.get("error_category"),
            details.get("http_status"), details.get("root_error_type"), details.get("root_error_message"))).encode()).hexdigest()[:16]
        with self.lock:
            state = self.components.setdefault(component, {})
            state.update(last_error_at=now, degraded=True, error_category=details.get("error_category"))
            first = fingerprint not in self.active
            if first:
                self.sequence += 1
                incident = dict(incident_id=f"i_{self.sequence}", component=component, operation=operation,
                    started_at=now, last_failure_at=now, failures=0, observations=0, last_summary_at=now,
                    **details)
                incident["seen"] = deque(maxlen=128)
                self.active[fingerprint] = incident
                self.incidents.append(incident)
                if len(self.active) > self.capacity:
                    self.active.popitem(last=False)
            incident = self.active[fingerprint]
            marker = (request_id, attempt)
            incident["observations"] += 1
            if not request_id or marker not in incident["seen"]:
                incident["failures"] += 1
                if request_id:
                    incident["seen"].append(marker)
            incident["last_failure_at"] = now
            summary = now - incident["last_summary_at"] >= self.summary_seconds
            if summary:
                incident["last_summary_at"] = now
            return dict(incident_id=incident["incident_id"], repeated=not first, failures=incident["failures"],
                        observations=incident["observations"], summary_due=summary, outage_duration_seconds=now-incident["started_at"])

    def success(self, component, operation=None, categories=None):
        now, recovered = self.clock(), []
        with self.lock:
            state = self.components.setdefault(component, {})
            state["last_success_at"] = now
            for key, incident in list(self.active.items()):
                if incident["component"] == component and (operation is None or incident["operation"] == operation) and (categories is None or incident.get('error_category') in categories):
                    incident.update(recovered_at=now, outcome="recovered")
                    recovered.append(dict(incident_id=incident["incident_id"], failures=incident["failures"],
                        previous_error_category=incident.get("error_category"), last_failure_at=incident["last_failure_at"],
                        outage_duration_seconds=now-incident["started_at"]))
                    del self.active[key]
            state["degraded"] = any(i["component"] == component for i in self.active.values())
            if recovered:
                state["last_recovery_at"] = now
        return recovered

    def update(self, component, **fields):
        with self.lock:
            self.components.setdefault(component, {}).update(fields)

    def increment(self, component, name, **fields):
        with self.lock:
            state = self.components.setdefault(component, {})
            state[name] = state.get(name, 0) + 1
            state.update(fields)

    def snapshot(self, errors=False):
        with self.lock:
            result = dict(process=deepcopy(self.process), components=deepcopy(self.components), active_incidents=len(self.active))
            if errors:
                result["incidents"] = [{k: deepcopy(v) for k, v in i.items() if k != "seen"} for i in list(self.incidents)[-10:]]
            return result


runtime_health = RuntimeHealth()
