import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


class UserGateClosed(RuntimeError):
    """Raised when a user operation starts after gate shutdown."""


class UserGateLifecycleError(RuntimeError):
    """Raised for cross-loop use or restart with live holders."""


class NestedUserGateAcquire(RuntimeError):
    """Raised when one task tries to acquire the same non-reentrant gate twice."""


@dataclass(slots=True)
class UserGateMetrics:
    contention_total: int = 0


@dataclass(slots=True)
class _UserGateEntry:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    references: int = 0
    waiters: int = 0
    held: bool = False


_held_user_ids: ContextVar[frozenset[int]] = ContextVar(
    "held_user_gate_ids", default=frozenset()
)


class UserOperationGate:
    """Short-lived, ref-counted, user-scoped async serialization gate."""

    def __init__(self) -> None:
        self._entries: dict[int, _UserGateEntry] = {}
        self._running = True
        self._loop = None
        self.metrics = UserGateMetrics()

    @property
    def registry_size(self) -> int:
        return len(self._entries)

    @property
    def waiter_count(self) -> int:
        return sum(entry.waiters for entry in self._entries.values())

    def held_by_current_task(self, user_id: int | None = None) -> bool:
        held = _held_user_ids.get()
        return bool(held) if user_id is None else int(user_id) in held

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise UserGateLifecycleError(
                "User operation gate belongs to another event loop; "
                "shutdown and start it before reuse"
            )

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._running:
            if self._loop is not None and self._loop is not loop:
                raise UserGateLifecycleError(
                    "Cannot move a running user operation gate to another loop"
                )
            self._loop = loop
            return
        if self._entries:
            raise UserGateLifecycleError(
                "Cannot restart user operation gate while holders are active"
            )
        self._loop = loop
        self._running = True

    def reset_for_tests(self) -> None:
        if self._entries:
            raise UserGateLifecycleError("Cannot reset user gate with live entries")
        self._loop = None
        self._running = True
        self.metrics.contention_total = 0

    async def shutdown(self) -> None:
        self._bind_loop()
        self._running = False
        for entry in tuple(self._entries.values()):
            async with entry.condition:
                entry.condition.notify_all()

    def _release_reference(self, user_id: int, entry: _UserGateEntry) -> None:
        entry.references -= 1
        if entry.references <= 0 and not entry.held and not entry.waiters:
            if self._entries.get(user_id) is entry:
                self._entries.pop(user_id, None)

    @asynccontextmanager
    async def hold(self, user_id: int):
        normalized_user_id = int(user_id)
        self._bind_loop()
        if not self._running:
            raise UserGateClosed("User operation gate is shut down")
        if normalized_user_id in _held_user_ids.get():
            raise NestedUserGateAcquire(
                f"User gate for {normalized_user_id} is non-reentrant"
            )

        entry = self._entries.get(normalized_user_id)
        if entry is None:
            entry = _UserGateEntry()
            self._entries[normalized_user_id] = entry
        entry.references += 1
        acquired = False
        token = None
        try:
            async with entry.condition:
                if entry.held:
                    entry.waiters += 1
                    self.metrics.contention_total += 1
                    try:
                        while entry.held and self._running:
                            await entry.condition.wait()
                    finally:
                        entry.waiters -= 1
                if not self._running:
                    raise UserGateClosed("User operation gate is shut down")
                entry.held = True
                acquired = True
            token = _held_user_ids.set(
                _held_user_ids.get() | {normalized_user_id}
            )
            yield
        finally:
            if token is not None:
                _held_user_ids.reset(token)
            if acquired:
                async with entry.condition:
                    entry.held = False
                    entry.condition.notify(1)
            self._release_reference(normalized_user_id, entry)


user_operation_gate = UserOperationGate()
