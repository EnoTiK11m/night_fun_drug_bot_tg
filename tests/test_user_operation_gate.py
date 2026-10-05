import asyncio
import unittest

from app.infrastructure.user_gate import (
    NestedUserGateAcquire,
    UserGateClosed,
    UserGateLifecycleError,
    UserOperationGate,
)


async def eventually(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class UserOperationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_is_serialized(self):
        gate = UserOperationGate()
        first_entered = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def first():
            async with gate.hold(1):
                order.append("first")
                first_entered.set()
                await release.wait()

        async def second():
            async with gate.hold(1):
                order.append("second")

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await eventually(lambda: gate.waiter_count == 1)
        self.assertEqual(order, ["first"])
        release.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(gate.registry_size, 0)

    async def test_different_users_run_in_parallel(self):
        gate = UserOperationGate()
        both_entered = asyncio.Event()
        entered = set()

        async def operation(user_id):
            async with gate.hold(user_id):
                entered.add(user_id)
                if len(entered) == 2:
                    both_entered.set()
                await both_entered.wait()

        await asyncio.wait_for(asyncio.gather(operation(1), operation(2)), 1)

    async def test_cancelled_waiter_is_removed(self):
        gate = UserOperationGate()
        release = asyncio.Event()

        async def holder():
            async with gate.hold(1):
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await eventually(lambda: gate.registry_size == 1)
        waiters = [asyncio.create_task(gate.hold(1).__aenter__()) for _ in range(3)]
        await eventually(lambda: gate.waiter_count == 3)
        for waiter in waiters:
            waiter.cancel()
        for waiter in waiters:
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        release.set()
        await holder_task
        self.assertEqual((gate.waiter_count, gate.registry_size), (0, 0))

    async def test_exception_releases_gate(self):
        gate = UserOperationGate()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with gate.hold(1):
                raise RuntimeError("boom")
        async with gate.hold(1):
            pass
        self.assertEqual(gate.registry_size, 0)

    async def test_thousands_of_users_leave_empty_registry(self):
        gate = UserOperationGate()
        for user_id in range(2000):
            async with gate.hold(user_id):
                pass
        self.assertEqual(gate.registry_size, 0)

    async def test_nested_same_user_is_rejected(self):
        gate = UserOperationGate()
        async with gate.hold(1):
            with self.assertRaises(NestedUserGateAcquire):
                async with gate.hold(1):
                    pass

    async def test_shutdown_releases_waiter_and_rejects_new_acquire(self):
        gate = UserOperationGate()
        release = asyncio.Event()

        async def holder():
            async with gate.hold(1):
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await eventually(lambda: gate.registry_size == 1)

        async def waiting():
            async with gate.hold(1):
                pass

        waiter = asyncio.create_task(waiting())
        await eventually(lambda: gate.waiter_count == 1)
        await gate.shutdown()
        with self.assertRaises(UserGateClosed):
            await waiter
        with self.assertRaises(UserGateClosed):
            async with gate.hold(2):
                pass
        release.set()
        await holder_task
        self.assertEqual(gate.registry_size, 0)


class UserOperationGateCrossLoopTests(unittest.TestCase):
    def test_shutdown_then_start_in_new_loop(self):
        gate = UserOperationGate()
        first_loop = asyncio.new_event_loop()
        second_loop = asyncio.new_event_loop()

        async def use(user_id):
            async with gate.hold(user_id):
                return True

        try:
            self.assertTrue(first_loop.run_until_complete(use(1)))
            with self.assertRaises(UserGateLifecycleError):
                second_loop.run_until_complete(use(2))
            first_loop.run_until_complete(gate.shutdown())
            second_loop.run_until_complete(gate.start())
            self.assertTrue(second_loop.run_until_complete(use(2)))
            second_loop.run_until_complete(gate.shutdown())
        finally:
            first_loop.close()
            second_loop.close()


if __name__ == "__main__":
    unittest.main()
