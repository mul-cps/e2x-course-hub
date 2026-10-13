"""Source lifecycle concurrency; HTTP authorization remains unchanged."""
import asyncio
import json
import unittest

import test_completion


class CourseStartConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_completion.CompletionTests.asyncSetUp(self)
        self.tasks = []
        workspace = await self.service.get('w')
        await self.provider.put('workspaces', {**workspace, 'namespace': 'trusted',
            'pod': 'preserved', 'course_ceiling': {}}, actor='admin')

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await test_completion.CompletionTests.asyncTearDown(self)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def add_workspace(self, identifier, group):
        if group != 'g':
            await self.provider.put('groups', {'id': group, 'course_id': 'c',
                'assignment_id': 'a'}, actor='admin')
            person = '00000000-0000-4000-8000-00000000000' + identifier[-1]
            await self.provider.link_identities([{'hub': 'cps', 'username': identifier,
                'email': identifier + '@example.edu', 'person_id': person,
                'verified': True}], actor='admin')
            await self.provider.put('memberships', {'id': 'm-' + identifier,
                'course_id': 'c', 'group_id': group, 'person_id': identifier,
                'canonical_person_id': person}, actor='admin')
        binding = {'group_id': group, 'hub_user': 'neutral-' + identifier,
            'hub_server': 'rtc-' + identifier, 'namespace': 'trusted', 'pod': 'preserved'}
        course = (await self.provider.courses())[0]
        await self.provider.put('courses', {**course,
            'workspace_bindings': course['workspace_bindings'] + [binding]}, actor='admin')
        await self.provider.put('workspaces', {'id': identifier, 'course_id': 'c',
            **binding, 'course_ceiling': {}, 'profile': 'cpu'}, actor='admin')

    async def test_distinct_groups_enter_spawn_before_either_is_ready(self):
        await self.add_workspace('w2', 'g2')
        ready = asyncio.Event()
        both_preview = asyncio.Event()
        both_spawn = asyncio.Event()
        previews, spawns = set(), set()
        original = self.service.compute.validate_workspace

        async def validate(*args):
            if len(args) == 4:
                previews.add(args[0])
                if len(previews) == 2:
                    both_preview.set()
                await both_preview.wait()
            return await original(*args)

        async def spawn(workspace):
            self.assertEqual((await self.service.get(workspace['id']))['state'], 'starting')
            spawns.add(workspace['id'])
            if len(spawns) == 2:
                both_spawn.set()
            await ready.wait()

        self.service.compute.validate_workspace = validate
        self.hub.start = spawn
        starts = [self.task(self.service.start(identifier, actor='teacher'))
                  for identifier in ('w', 'w2')]
        await asyncio.wait_for(both_spawn.wait(), 1)
        self.assertEqual(spawns, {'w', 'w2'})
        self.assertFalse(any(task.done() for task in starts))
        ready.set()
        await asyncio.wait_for(asyncio.gather(*starts), 1)
        self.assertEqual([w['state'] for w in await self.provider.list('workspaces')],
                         ['running', 'running'])
        guards = [row for row in self.provider.audit() if row['kind'] == 'workspaces'
                  and row['current'] and json.loads(row['current']).get('state') == 'starting']
        self.assertEqual({row['target'] for row in guards}, {'w', 'w2'})

    async def test_same_workspace_start_waits_and_cannot_spawn_twice(self):
        entered, ready = asyncio.Event(), asyncio.Event()
        spawns = []

        async def spawn(workspace):
            spawns.append(workspace['id'])
            entered.set()
            await ready.wait()

        self.hub.start = spawn
        first = self.task(self.service.start('w', actor='teacher'))
        await entered.wait()
        second = self.task(self.service.start('w', actor='teacher'))
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        ready.set()
        await first
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            await second
        self.assertEqual(spawns, ['w'])

    async def test_same_group_start_rejects_an_unresolved_sibling(self):
        await self.add_workspace('w2', 'g')
        entered, ready = asyncio.Event(), asyncio.Event()

        async def spawn(workspace):
            entered.set()
            await ready.wait()

        self.hub.start = spawn
        self.task(self.service.start('w', actor='teacher'))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, 'Shared group'):
            await asyncio.wait_for(self.service.start('w2', actor='teacher'), 1)

    async def test_queued_removal_waits_for_failed_start_cleanup_and_blocks_new_start(self):
        await self.add_workspace('w2', 'g2')
        spawned, fail, cleaning, cleaned = [asyncio.Event() for _ in range(4)]
        new_spawn = asyncio.Event()
        original_stop = self.hub.stop_confirmed

        async def spawn(workspace):
            if workspace['id'] == 'w':
                spawned.set()
                await fail.wait()
                raise RuntimeError('lost spawn response')
            new_spawn.set()

        async def stop(workspace):
            if workspace['id'] == 'w' and not cleaned.is_set():
                cleaning.set()
                await cleaned.wait()
            await original_stop(workspace)

        self.hub.start, self.hub.stop_confirmed = spawn, stop
        start = self.task(self.service.start('w', actor='teacher'))
        await spawned.wait()
        fail.set()
        await cleaning.wait()
        removal = self.task(self.service.remove_member('w', 'm', actor='teacher'))
        await asyncio.sleep(0)
        late = self.task(self.service.start('w2', actor='teacher'))
        await asyncio.sleep(0)
        self.assertFalse(new_spawn.is_set())
        self.assertNotIn('remove', self.calls)
        cleaned.set()
        with self.assertRaisesRegex(RuntimeError, 'lost spawn'):
            await start
        await asyncio.wait_for(asyncio.gather(removal, late), 1)
        self.assertTrue(new_spawn.is_set())
        self.assertEqual([m['id'] for m in await self.provider.members('c')], ['m-w2'])

    async def test_scoped_guard_ignores_only_other_group_state_and_notice(self):
        await self.add_workspace('w2', 'g2')
        workspace = await self.service.get('w')
        scoped = self.provider.lifecycle_snapshot('c', workspace_id='w')
        full = self.provider.lifecycle_snapshot('c')
        other = await self.service.get('w2')
        await self.provider.put('workspaces', {**other, 'state': 'starting',
            'notice': 'waiting for readiness'}, actor='teacher')
        with self.assertRaisesRegex(ValueError, 'authority changed'):
            await self.provider.put('workspaces', {**workspace, 'state': 'starting'},
                actor='teacher', expected_lifecycle=full)
        await self.provider.put('workspaces', {**workspace, 'state': 'starting'},
            actor='teacher', expected_lifecycle=scoped)
        self.assertEqual((await self.service.get('w'))['state'], 'starting')

    async def test_scoped_guard_preserves_target_sibling_and_other_group_authority(self):
        await self.add_workspace('w2', 'g')
        await self.add_workspace('w3', 'g3')
        for identifier, field, value in (
                ('w', 'state', 'starting'), ('w2', 'state', 'starting'),
                ('w3', 'profile', 'other'), ('w3', 'namespace', 'foreign'),
                ('w3', 'archive_pending', True)):
            with self.subTest(identifier=identifier, field=field):
                target = await self.service.get('w')
                original = await self.service.get(identifier)
                expected = self.provider.lifecycle_snapshot('c', workspace_id='w')
                await self.provider.put('workspaces', {**original, field: value}, actor='admin')
                with self.assertRaisesRegex(ValueError, 'authority changed'):
                    await self.provider.put('workspaces', {**target, 'state': 'starting'},
                        actor='teacher', expected_lifecycle=expected)
                await self.provider.put('workspaces', original, actor='admin')

    async def test_scoped_guard_keeps_membership_and_identity_changes_authoritative(self):
        workspace = await self.service.get('w')
        expected = self.provider.lifecycle_snapshot('c', workspace_id='w')
        membership = (await self.provider.members('c'))[0]
        await self.provider.put('memberships', {**membership, 'expires': '2030-01-01T00:00:00Z'},
            actor='admin')
        with self.assertRaisesRegex(ValueError, 'authority changed'):
            await self.provider.put('workspaces', {**workspace, 'state': 'starting'},
                actor='teacher', expected_lifecycle=expected)
        expected = self.provider.lifecycle_snapshot('c', workspace_id='w')
        with self.provider.db:
            self.provider.db.execute("UPDATE email_links SET verified=0 WHERE console='cps'")
        with self.assertRaisesRegex(ValueError, 'authority changed'):
            await self.provider.put('workspaces', {**workspace, 'state': 'starting'},
                actor='teacher', expected_lifecycle=expected)

    async def test_scoped_guard_has_one_same_target_cross_instance_winner(self):
        from e2x_course_hub.cps.providers import LocalCourseProvider
        other = LocalCourseProvider(self.provider.db.execute('PRAGMA database_list').fetchone()[2], 'cps')
        workspace = await self.service.get('w')
        expected = self.provider.lifecycle_snapshot('c', workspace_id='w')
        try:
            await self.provider.put('workspaces', {**workspace, 'state': 'starting'},
                actor='teacher', expected_lifecycle=expected)
            with self.assertRaisesRegex(ValueError, 'authority changed'):
                await other.put('workspaces', {**workspace, 'state': 'starting'},
                    actor='other-teacher', expected_lifecycle=expected)
            self.assertEqual((await self.service.get('w'))['state'], 'starting')
            self.assertFalse(other.db.in_transaction)
            self.assertTrue(any(row['actor'] == 'other-teacher' and row['outcome'] == 'failure'
                                for row in other.audit()))
        finally:
            other.db.close()

    async def test_cancelled_spawn_releases_reader_but_removal_still_stops_writer(self):
        entered = asyncio.Event()
        async def spawn(workspace):
            entered.set()
            await asyncio.Event().wait()
        self.hub.start = spawn
        start = self.task(self.service.start('w', actor='teacher'))
        await entered.wait()
        start.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start
        self.assertEqual((await self.service.get('w'))['state'], 'starting')
        await asyncio.wait_for(self.service.remove_member('w', 'm', actor='teacher'), 1)
        self.assertLess(self.calls.index('stop:w'), self.calls.index('remove'))
        self.assertLess(self.calls.index('release'), self.calls.index('shares'))
        self.assertEqual(await self.provider.members('c'), [])


class CourseOperationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_writer_blocks_new_readers(self):
        from e2x_course_hub.cps.course_locks import CourseOperationLock
        lock = CourseOperationLock()
        release_reader, release_writer = asyncio.Event(), asyncio.Event()
        reader_entered, writer_entered, late_entered = [asyncio.Event() for _ in range(3)]
        order = []

        async def reader():
            async with lock.read():
                reader_entered.set()
                await release_reader.wait()

        async def writer():
            async with lock.write():
                order.append('writer')
                writer_entered.set()
                await release_writer.wait()

        async def late_reader():
            async with lock.read():
                order.append('reader')
                late_entered.set()

        first = asyncio.create_task(reader())
        await reader_entered.wait()
        queued = asyncio.create_task(writer())
        await asyncio.sleep(0)
        late = asyncio.create_task(late_reader())
        await asyncio.sleep(0)
        self.assertFalse(late_entered.is_set())
        release_reader.set()
        await writer_entered.wait()
        self.assertFalse(late_entered.is_set())
        release_writer.set()
        await asyncio.wait_for(asyncio.gather(first, queued, late), 1)
        self.assertEqual(order, ['writer', 'reader'])

    async def test_cancelled_waiting_writer_unblocks_readers(self):
        from e2x_course_hub.cps.course_locks import CourseOperationLock
        lock = CourseOperationLock()
        async with lock.read():
            async def write():
                async with lock.write():
                    self.fail('writer entered while reader active')
            queued = asyncio.create_task(write())
            await asyncio.sleep(0)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            async def read():
                async with lock.read():
                    pass
            await asyncio.wait_for(read(), 1)
        async with lock.write():
            pass

    async def test_cancelled_waiting_reader_and_active_reader_leave_no_leases(self):
        from e2x_course_hub.cps.course_locks import CourseOperationLock
        lock = CourseOperationLock()
        entered = asyncio.Event()
        async def read():
            async with lock.read():
                entered.set()
                await asyncio.Event().wait()
        async with lock.write():
            waiting = asyncio.create_task(read())
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
        active = asyncio.create_task(read())
        await entered.wait()
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        async def write():
            async with lock.write():
                pass
        await asyncio.wait_for(write(), 1)

    async def test_cancelled_active_writer_releases_exclusive_lease(self):
        from e2x_course_hub.cps.course_locks import CourseOperationLock
        lock = CourseOperationLock()
        entered = asyncio.Event()
        async def write():
            async with lock.write():
                entered.set()
                await asyncio.Event().wait()
        active = asyncio.create_task(write())
        await entered.wait()
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        async def read():
            async with lock.read():
                pass
        await asyncio.wait_for(read(), 1)
