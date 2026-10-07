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

    @staticmethod
    def _operation_local(component, details):
        # A rejected payload says nothing about transport availability. Keep
        # its bounded aggregation, without treating it as a subsystem outage.
        return component == 'telegram' and details.get('error_category') == 'telegram_bad_request'

    def _refresh_degraded(self, component):
        state = self.components.setdefault(component, {})
        state['degraded'] = any(i['component'] == component and i['scope'] == 'subsystem'
                                for i in self.active.values()) or (
            component == 'telegram' and state.get('cooldown_remaining_seconds', 0) > 0)

    def error(self, component, operation, details, request_id=None, attempt=None):
        now = self.clock()
        fingerprint = hashlib.sha256(repr((component, operation, details.get("error_category"),
            details.get("http_status"), details.get("root_error_type"), details.get("root_error_message"))).encode()).hexdigest()[:16]
        with self.lock:
            state = self.components.setdefault(component, {})
            state.update(last_error_at=now, error_category=details.get("error_category"))
            first = fingerprint not in self.active
            if first:
                self.sequence += 1
                incident = dict(incident_id=f"i_{self.sequence}", component=component, operation=operation,
                    started_at=now, last_failure_at=now, failures=0, observations=0, last_summary_at=now,
                    **details)
                incident["seen"] = deque(maxlen=128)
                incident['scope'] = 'operation_local' if self._operation_local(component, details) else 'subsystem'
                if incident['scope'] == 'operation_local':
                    incident['outcome'] = 'operation_local'
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
            self._refresh_degraded(component)
            return dict(incident_id=incident["incident_id"], repeated=not first, failures=incident["failures"],
                        observations=incident["observations"], summary_due=summary, scope=incident['scope'],
                        outage_duration_seconds=now-incident["started_at"])

    def success(self, component, operation=None, categories=None):
        now, recovered = self.clock(), []
        with self.lock:
            state = self.components.setdefault(component, {})
            state["last_success_at"] = now
            for key, incident in list(self.active.items()):
                if incident['scope'] == 'operation_local':
                    continue  # Unrelated success cannot validate this payload.
                if incident.get('error_category') == 'telegram_retry_after' and state.get('cooldown_remaining_seconds', 0) > 0:
                    continue
                if incident["component"] == component and (operation is None or incident["operation"] == operation) and (categories is None or incident.get('error_category') in categories):
                    incident.update(recovered_at=now, outcome="recovered")
                    recovered.append(dict(incident_id=incident["incident_id"], failures=incident["failures"],
                        previous_error_category=incident.get("error_category"), last_failure_at=incident["last_failure_at"],
                        outage_duration_seconds=now-incident["started_at"]))
                    del self.active[key]
            self._refresh_degraded(component)
            if recovered:
                state["last_recovery_at"] = now
        return recovered

    def update(self, component, **fields):
        with self.lock:
            self.components.setdefault(component, {}).update(fields)
            if component == 'telegram':
                self._refresh_degraded(component)

    def increment(self, component, name, **fields):
        with self.lock:
            state = self.components.setdefault(component, {})
            state[name] = state.get(name, 0) + 1
            state.update(fields)

    def snapshot(self, errors=False):
        with self.lock:
            result = dict(process=deepcopy(self.process), components=deepcopy(self.components),
                          active_incidents=sum(i['scope'] == 'subsystem' for i in self.active.values()))
            if errors:
                result["incidents"] = [{k: deepcopy(v) for k, v in i.items() if k != "seen"} for i in list(self.incidents)[-10:]]
            return result


runtime_health = RuntimeHealth()
