"""In-process course lifecycle exclusion; queued mutations take priority."""
import asyncio
from contextlib import asynccontextmanager


class CourseOperationLock:
    def __init__(self):
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and not self._waiting_writers)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer and not self._readers)
                self._writer = True
            finally:
                # Cancellation while queued must not strand later readers.
                self._waiting_writers -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()
