# Concord
#
# Copyright (c) 2019 VMware, Inc. All Rights Reserved.
#
# This product is licensed to you under the Apache 2.0 license (the "License").
# You may not use this product except in compliance with the Apache 2.0 License.
#
# This product may include a number of subcomponents with separate copyright
# notices and license terms. Your use of these subcomponents is subject to the
# terms and conditions of the subcomponent's license, as noted in the LICENSE
# file.

import os.path
import random
import unittest
from os import environ

import trio

from util import bft_network_partitioning as net
from util import skvbc as kvbc
from util.bft import with_trio, with_bft_network, KEY_FILE_PREFIX
from util.skvbc_history_tracker import verify_linearizability

def start_replica_cmd(builddir, replica_id):
    """
    Return a command that starts an skvbc replica when passed to
    subprocess.Popen.
    Note each arguments is an element in a list.
    """
    statusTimerMilli = "500"
    viewChangeTimeoutMilli = "10000"
    path = os.path.join(builddir, "tests", "simpleKVBC", "TesterReplica", "skvbc_replica")
    return [path,
            "-k", KEY_FILE_PREFIX,
            "-i", str(replica_id),
            "-s", statusTimerMilli,
            "-v", viewChangeTimeoutMilli,
            "-p" if os.environ.get('BUILD_ROCKSDB_STORAGE', "").lower()
                    in set(["true", "on"])
                 else "",
            "-t", os.environ.get('STORAGE_TYPE')]


class SkvbcViewChangeTest(unittest.TestCase):

    __test__ = False  # so that PyTest ignores this test scenario

    @with_trio
    @with_bft_network(start_replica_cmd)
    async def test_request_block_not_written_primary_down(self, bft_network):
        """
        The goal of this test is to validate that a block wasn't written,
        if the primary fell down.

        1) Given a BFT network, we trigger one write.
        2) Make sure the initial view is preserved during this write.
        3) Stop the primary replica and send a batch of write requests.
        4) Verify the BFT network eventually transitions to the next view.
        5) Validate that there is no new block written.
        """
        bft_network.start_all_replicas()

        client = bft_network.random_client()

        skvbc = kvbc.SimpleKVBCProtocol(bft_network)

        initial_primary = 0
        expected_next_primary = 1

        key_before_vc, value_before_vc = await skvbc.write_known_kv()

        await skvbc.assert_kv_write_executed(key_before_vc, value_before_vc)

        await bft_network.wait_for_view(
            replica_id=initial_primary,
            expected=lambda v: v == initial_primary,
            err_msg="Make sure we are in the initial view "
                    "before crashing the primary."
        )

        last_block = skvbc.parse_reply(await client.read(skvbc.get_last_block_req()))

        bft_network.stop_replica(initial_primary)
        try:
            with trio.move_on_after(seconds=1):  # seconds
                await skvbc.send_indefinite_write_requests()

        except trio.TooSlowError:
            pass
        finally:

            await bft_network.wait_for_view(
                replica_id=random.choice(bft_network.all_replicas(without={0})),
                expected=lambda v: v == expected_next_primary,
                err_msg="Make sure view change has been triggered."
            )

            new_last_block = skvbc.parse_reply(await client.read(skvbc.get_last_block_req()))
            self.assertEqual(new_last_block, last_block)



    @with_trio
    @with_bft_network(start_replica_cmd)
    @verify_linearizability()
    async def test_single_vc_only_primary_down(self, bft_network, tracker):
        """
        The goal of this test is to validate the most basic view change
        scenario - a single view change when the primary replica is down.

        1) Given a BFT network, we trigger parallel writes.
        2) Make sure the initial view is preserved during those writes.
        3) Stop the primary replica and send a batch of write requests.
        4) Verify the BFT network eventually transitions to the next view.
        5) Perform a "read-your-writes" check in the new view
        """
        await self._single_vc_with_consecutive_failed_replicas(
            bft_network,
            tracker,
            num_consecutive_failing_primaries=1
        )

    @with_trio
    @with_bft_network(start_replica_cmd)
    @verify_linearizability()
    async def test_single_vc_with_f_replicas_down(self, bft_network, tracker):
        """
        Here we "step it up" a little bit, bringing down a total of f replicas
        (including the primary), verifying a single view change in this scenario.

        1) Given a BFT network, we make sure all nodes are up.
        2) crash f replicas, including the current primary.
        3) Trigger parallel requests to start the view change.
        4) Verify the BFT network eventually transitions to the next view.
        5) Perform a "read-your-writes" check in the new view
        """
        bft_network.start_all_replicas()

        n = bft_network.config.n
        f = bft_network.config.f
        c = bft_network.config.c

        self.assertEqual(len(bft_network.procs), n,
                         "Make sure all replicas are up initially.")
        initial_primary = 0
        expected_next_primary = 1

        crashed_replicas = await self._crash_replicas_including_primary(
            bft_network=bft_network,
            nb_crashing=f,
            primary=initial_primary,
            except_replicas={expected_next_primary}
        )
        self.assertFalse(expected_next_primary in crashed_replicas)

        self.assertGreaterEqual(
            len(bft_network.procs), 2 * f + 2 * c + 1,
            "Make sure enough replicas are up to allow a successful view change")

        await self._send_random_writes(tracker)

        await bft_network.wait_for_view(
            replica_id=random.choice(bft_network.all_replicas(without=crashed_replicas)),
            expected=lambda v: v == expected_next_primary,
            err_msg="Make sure view change has been triggered."
        )

        await self._wait_for_read_your_writes_success(tracker)

        await tracker.run_concurrent_ops(100)

    @with_trio
    @with_bft_network(start_replica_cmd, selected_configs=lambda n, f, c: f >= 2)
    @verify_linearizability()
    async def test_crashed_replica_catch_up_after_view_change(self, bft_network, tracker):
        """
        In this test scenario we want to make sure that a crashed replica
        that missed a view change will catch up and start working within
        the new view once it is restarted:
        1) Start all replicas
        2) Send a batch of concurrent reads/writes, to make sure the initial view is stable
        3) Choose a random non-primary and crash it
        4) Crash the current primary & trigger view change
        5) Make sure the new view is agreed & activated among all live replicas
        6) Start the crashed replica
        7) Make sure the crashed replica works in the new view

        Note: this scenario requires f >= 2, because at certain moments we have
        two simultaneously crashed replicas (the primary and the non-primary that is
        missing the view change).
        """
        bft_network.start_all_replicas()

        initial_primary = 0
        expected_next_primary = 1

        await tracker.run_concurrent_ops(num_ops=50)

        unstable_replica = random.choice(
            bft_network.all_replicas(without={initial_primary, expected_next_primary}))

        await bft_network.wait_for_view(
            replica_id=unstable_replica,
            expected=lambda v: v == initial_primary,
            err_msg="Make sure the unstable replica works in the initial view."
        )

        print(f"Crash replica #{unstable_replica} before the view change.")
        bft_network.stop_replica(unstable_replica)

        # trigger a view change
        bft_network.stop_replica(initial_primary)
        await self._send_random_writes(tracker)

        await bft_network.wait_for_view(
            replica_id=random.choice(bft_network.all_replicas(
                without={initial_primary, unstable_replica})),
            expected=lambda v: v == expected_next_primary,
            err_msg="Make sure view change has been triggered."
        )

        # waiting for the active window to be rebuilt after the view change
        await trio.sleep(seconds=10)

        # restart the unstable replica and make sure it works in the new view
        bft_network.start_replica(unstable_replica)
        await tracker.run_concurrent_ops(num_ops=50)

        await bft_network.wait_for_view(
            replica_id=unstable_replica,
            expected=lambda v: v == expected_next_primary,
            err_msg="Make sure the unstable replica works in the new view."
        )

    @with_trio
    @with_bft_network(start_replica_cmd)
    @verify_linearizability()
    async def test_restart_replica_after_view_change(self, bft_network, tracker):
        """
        This test makes sure that a replica can be safely restarted after a view change:
        1) Start all replicas
        2) Send a batch of concurrent reads/writes, to make sure the initial view is stable
        3) Crash the current primary & trigger view change
        4) Make sure the new view is agreed & activated among all live replicas
        5) Choose a random non-primary and restart it
        6) Send a batch of concurrent reads/writes
        7) Make sure the restarted replica is alive and that it works in the new view
        """
        bft_network.start_all_replicas()
        initial_primary = 0

        await tracker.run_concurrent_ops(num_ops=50)

        bft_network.stop_replica(initial_primary)
        await self._send_random_writes(tracker)

        await bft_network.wait_for_view(
            replica_id=random.choice(
                bft_network.all_replicas(without={initial_primary})),
            expected=lambda v: v == initial_primary + 1,
            err_msg="Make sure a view change is triggered."
        )
        current_primary = initial_primary + 1

        bft_network.start_replica(initial_primary)

        # waiting for the active window to be rebuilt after the view change
        await trio.sleep(seconds=10)

        unstable_replica = random.choice(
            bft_network.all_replicas(without={current_primary, initial_primary}))
        print(f"Restart replica #{unstable_replica} after the view change.")

        bft_network.stop_replica(unstable_replica)
        bft_network.start_replica(unstable_replica)
        await trio.sleep(seconds=5)

        await tracker.run_concurrent_ops(num_ops=50)

        await bft_network.wait_for_view(
            replica_id=unstable_replica,
            expected=lambda v: v >= current_primary,  # the >= is required because of https://jira.eng.vmware.com/browse/BC-3028
            err_msg="Make sure the unstable replica works in the new view."
        )

        await bft_network.wait_for_view(
            replica_id=initial_primary,
            expected=lambda v: v >= current_primary,  # the >= is required because of https://jira.eng.vmware.com/browse/BC-3028
            err_msg="Make sure the initial primary activates the new view."
        )

    @unittest.skip("unstable scenario")
    @with_trio
    @with_bft_network(start_replica_cmd,
                      selected_configs=lambda n, f, c: c < f)
    @verify_linearizability()
    async def test_multiple_vc_slow_path(self, bft_network, tracker):
        """
        In this test we aim to validate a sequence of view changes,
        maintaining the slow commit path. To do so, we need to crash
        at least c+1 replicas, including the primary.

        1) Given a BFT network, we make sure all nodes are up.
        2) Repeat the following several times:
            2.1) Crash c+1 replicas (including the primary)
            2.2) Send parallel requests to start the view change.
            2.3) Verify the BFT network eventually transitions to the next view.
            2.4) Perform a "read-your-writes" check in the new view
        3) Make sure the slow path was prevalent during all view changes

        Note: we require that c < f because:
        A) for view change we need at least n-f = 2f+2c+1 replicas
        B) to ensure transition to the slow path, we need to crash at least c+1 replicas.
        Combining A) and B) yields n-(c+1) >= 2f+2c+1, equivalent to c < f
        """
        bft_network.start_all_replicas()

        n = bft_network.config.n
        f = bft_network.config.f
        c = bft_network.config.c

        current_primary = 0
        for _ in range(2):
            self.assertEqual(len(bft_network.procs), n,
                             "Make sure all replicas are up initially.")

            expected_next_primary = current_primary + 1
            crashed_replicas = await self._crash_replicas_including_primary(
                bft_network=bft_network,
                nb_crashing=c+1,
                primary=current_primary,
                except_replicas={expected_next_primary}
            )
            self.assertFalse(expected_next_primary in crashed_replicas)

            self.assertGreaterEqual(
                len(bft_network.procs), 2 * f + 2 * c + 1,
                "Make sure enough replicas are up to allow a successful view change")

            await self._send_random_writes(tracker)

            stable_replica = random.choice(
                bft_network.all_replicas(without=crashed_replicas))

            view = await bft_network.wait_for_view(
                replica_id=stable_replica,
                expected=lambda v: v >= expected_next_primary,
                err_msg="Make sure a view change has been triggered."
            )
            current_primary = view
            [bft_network.start_replica(i) for i in crashed_replicas]

        await tracker.tracked_read_your_writes()
  
        await bft_network.wait_for_view(
            replica_id=current_primary,
            err_msg="Make sure all ongoing view changes have completed."
        )

        await tracker.tracked_read_your_writes()

        await bft_network.wait_for_slow_path_to_be_prevalent(
            replica_id=current_primary)

    @with_trio
    @with_bft_network(start_replica_cmd,
                      selected_configs = lambda n,f,c : f >= 2)
    @verify_linearizability()
    async def test_single_vc_current_and_next_primaries_down(self, bft_network, tracker):
        """
        The goal of this test is to validate the skip view scenario, where 
        both the primary and the expected next primary have failed. In this 
        case the first view change does not happen, causing timers in the 
        replicas to expire and a view change to v+2 is initiated.

        1) Given a BFT network, we trigger parallel writes.
        2) Make sure the initial view is preserved during those writes.
        3) Stop the primary and next primary replica and send a batch of write requests.
        4) Verify the BFT network eventually transitions to the expected view (v+2).
        5) Perform a "read-your-writes" check in the new view.
        6) We have to filter only configurations which support more than 2 faulty replicas.
        """
        
        await self._single_vc_with_consecutive_failed_replicas(
            bft_network,
            tracker,
            num_consecutive_failing_primaries = 2
        )

    async def _single_vc_with_consecutive_failed_replicas(
            self,
            bft_network,
            tracker,
            num_consecutive_failing_primaries):

        bft_network.start_all_replicas()

        initial_primary = await bft_network.get_current_primary()
        initial_view = await bft_network.get_current_view()
        replcas_to_stop = [ v for v in range(initial_primary,
                                             initial_primary + num_consecutive_failing_primaries) ]
        
        expected_final_view = initial_view + num_consecutive_failing_primaries

        await self._send_random_writes(tracker)

        await bft_network.wait_for_view(
            replica_id=initial_primary,
            expected=lambda v: v == initial_view,
            err_msg="Make sure we are in the initial view "
                    "before crashing the primary."
        )

        for replica in replcas_to_stop:
            bft_network.stop_replica(replica)

        await self._send_random_writes(tracker)

        await bft_network.wait_for_view(
            replica_id=random.choice(bft_network.all_replicas(without=set(replcas_to_stop))),
            expected=lambda v: v == expected_final_view,
            err_msg="Make sure view change has been triggered."
        )

        await self._wait_for_read_your_writes_success(tracker)

        await tracker.run_concurrent_ops(100)

    async def _wait_for_read_your_writes_success(self, tracker):
        with trio.fail_after(seconds=60):
            while True:
                with trio.move_on_after(seconds=5):
                    try:
                        await tracker.tracked_read_your_writes()
                    except Exception:
                        continue
                    else:
                        break

    async def _send_random_writes(self, tracker):
        with trio.move_on_after(seconds=1):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(tracker.send_indefinite_tracked_ops, 1)

    async def _crash_replicas_including_primary(
            self, bft_network, nb_crashing, primary, except_replicas=None):
        if except_replicas is None:
            except_replicas = set()

        crashed_replicas = set()

        bft_network.stop_replica(primary)
        crashed_replicas.add(primary)

        crash_candidates = bft_network.all_replicas(
            without=except_replicas.union({primary}))
        random.shuffle(crash_candidates)
        for i in range(nb_crashing - 1):
            bft_network.stop_replica(crash_candidates[i])
            crashed_replicas.add(crash_candidates[i])

        return crashed_replicas
