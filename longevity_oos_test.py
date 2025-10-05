#!/usr/bin/env python

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2025 ScyllaDB
import contextlib
from itertools import cycle
from contextlib import ExitStack, contextmanager
from time import sleep, time
from longevity_test import LongevityTest
from sdcm.cluster import MAX_TIME_WAIT_FOR_DECOMMISSION, MAX_TIME_WAIT_FOR_NEW_NODE_UP, BaseNode
from sdcm.db_stats import PrometheusDBStats
from sdcm.exceptions import WaitForTimeoutError
from sdcm.mgmt.cli import RepairTask
from sdcm.mgmt.common import ScyllaManagerError, TaskStatus
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent, CassandraStressLogEvent
from sdcm.sct_events.system import InfoEvent, TestFrameworkEvent
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.common import ParallelObject
from sdcm.utils.decorators import retrying
from sdcm.utils.nemesis_utils.indexes import create_index, drop_index, verify_query_by_index_works, wait_for_index_to_be_built
from sdcm.utils.tablets.common import wait_no_tablets_migration_running
from threading import Thread


@contextmanager
def ignore_stress_errors():
    # When the out of space controller is active, it will reject writes and
    # cassandra-stress will log errors and fail.
    with ExitStack() as stack:
        stack.enter_context(EventsSeverityChangerFilter(
            new_severity=Severity.NORMAL,
            event_class=CassandraStressEvent,
            extra_time_to_expiration=60
        ))
        stack.enter_context(EventsSeverityChangerFilter(
            new_severity=Severity.NORMAL,
            event_class=CassandraStressLogEvent,
            extra_time_to_expiration=60
        ))
        yield


class LongevityOutOfSpaceTest(LongevityTest):
    def tearDown(self):
        # an extra failure check for disk usage
        for node in self.db_cluster.nodes:
            max_usage = self.get_node_max_disk_usage(node, start=self.start_time, end=time())
            if max_usage >= 98.5:
                TestFrameworkEvent(source=self.__class__.__name__,
                                   message=f"Node {node.name} ({node.private_ip_address}) max disk usage was {max_usage:.2f}%.",
                                   severity=Severity.ERROR).publish()

        super().tearDown()

    def _query_disk_usage(self, node: BaseNode, start: float = None, end: float = None) -> float:
        """
        :param node: The node to get the disk usage for.
        :param start: The start time for the query as a timestamp. Defaults to end - 60.
        :param end: The end time for the query as a timestamp. Defaults to current time.
        :return: The results of the query.
        """
        self.prometheus_db: PrometheusDBStats
        end = end or time()
        start = start or (end - 60)
        avail_query = f'sum(node_filesystem_avail_bytes{{mountpoint="/var/lib/scylla", instance=~".*?{node.private_ip_address}.*?", job=~"node_exporter.*"}})'
        size_query = f'sum(node_filesystem_size_bytes{{mountpoint="/var/lib/scylla", instance=~".*?{node.private_ip_address}.*?", job=~"node_exporter.*"}})'
        full_query = f'100 * (1 - ({avail_query} / {size_query}))'
        return self.prometheus_db.query(query=full_query, start=start, end=end)

    def get_disk_usage(self, node: BaseNode) -> float:
        """
        :param node: The node to get the disk usage for.
        :return: The disk usage in percentage, or -1 if the query fails.
        """
        results = self._query_disk_usage(node)

        try:
            return float(results[0]['values'][-1][1])
        except (IndexError, ValueError, TypeError):
            # Catch any errors in case the results are malformed
            return -1

    @retrying(n=3, sleep_time=60)
    def get_node_max_disk_usage(self, node: BaseNode, start: float, end: float) -> float:
        """
        :param node: The node to get the max disk usage for.
        :param start: The start time for the query as a timestamp.
        :param end: The end time for the query as a timestamp.
        :return: The max disk usage in percentage.
        """
        results = self._query_disk_usage(node, start=start, end=end)
        return max(float(v[1]) for v in results[0]['values'])

    def error_event(self, message):
        TestFrameworkEvent(source=self.__class__.__name__, message=message, severity=Severity.ERROR).publish()

    def run_read_stress(self, want_success=True):
        stress_queue = []
        stress_cmd = self.params.get('stress_cmd_r')
        keyspace_num = self.params.get('keyspace_num')

        with (contextlib.nullcontext() if want_success else ignore_stress_errors()):
            self.assemble_and_run_all_stress_cmd(stress_queue, stress_cmd, keyspace_num)
            if all(self.verify_stress_thread(stress) for stress in stress_queue) != want_success:
                self.error_event(
                    "Read should have succeeded, but it failed" if want_success else "Read should have failed, but it succeeded")

    def run_write_stress(self, want_success=True):
        stress_queue = []
        stress_cmd = self.params.get('stress_cmd_w')
        keyspace_num = self.params.get('keyspace_num')

        with (contextlib.nullcontext() if want_success else ignore_stress_errors()):
            self.assemble_and_run_all_stress_cmd(stress_queue, stress_cmd, keyspace_num)
            if all(self.verify_stress_thread(stress) for stress in stress_queue) != want_success:
                self.error_event(
                    "Write should have succeeded, but it failed" if want_success else "Write should have failed, but it succeeded")

    def scale_out(self):
        """
        Scale out the cluster by adding new nodes.
        """
        added_nodes = self.db_cluster.add_nodes(
            count=self.db_cluster.racks_count,
            instance_type=self.params.get("instance_type_db"),
            enable_auto_bootstrap=True,
            rack=None)
        self.monitors.reconfigure_scylla_monitoring()
        up_timeout = MAX_TIME_WAIT_FOR_NEW_NODE_UP
        with adaptive_timeout(Operations.NEW_NODE, node=self.db_cluster.data_nodes[0], timeout=up_timeout):
            self.db_cluster.wait_for_init(node_list=added_nodes, timeout=up_timeout, check_node_health=False)
        self.db_cluster.set_seeds()
        self.db_cluster.update_seed_provider()
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=added_nodes)
        for node in self.db_cluster.nodes:
            wait_no_tablets_migration_running(node, timeout=7200)
        return added_nodes

    def scale_in(self, nodes: list[BaseNode]):
        """
        Scale in the cluster by decommissioning the specified nodes.
        :param nodes: The nodes to decommission.
        """
        for node in nodes:
            self.nemesis_allocator.set_running_nemesis(node, 'decommissioning')
        parallel_obj = ParallelObject(objects=nodes, timeout=MAX_TIME_WAIT_FOR_DECOMMISSION, num_workers=len(nodes))
        InfoEvent(f'Started decommissioning {[node for node in nodes]}').publish()
        parallel_obj.run(self.db_cluster.decommission, ignore_exceptions=False, unpack_objects=True)
        InfoEvent(f'Finished decommissioning {[node for node in nodes]}').publish()
        self.monitors.reconfigure_scylla_monitoring()

    def get_compactions(self, node: BaseNode, interval: int) -> int:
        """
        Get the number of compactions that occurred on the node during the specified interval.

        :param node: The node to get the compactions for.
        :param interval: The time interval for the query, as last X seconds.
        :return: The number of compactions that occurred on the node during the specified interval.
        """
        compaction_query = f'sum(scylla_compaction_manager_compactions{{instance=~".*?{node.private_ip_address}.*?"}})'
        now = time()
        results = self.prometheus_db.query(query=compaction_query, start=now - interval, end=now)
        self.log.info(f"Compactions on node {node.name} ({node.private_ip_address}): {results}")
        # results is of this form:
        # [{'metric': {}, 'values': [[1749638594.936, '0'], [1749638614.936, '0'], [1749638634.936, '0'], [1749638654.936, '0']]}]
        return sum(int(v[1]) for v in results[0]['values']) if results else 0

    def drop_new_keyspace(self):
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute('DROP KEYSPACE keyspace2')

    def prepare_with_restarts(self):
        prepare_thread = Thread(target=self.run_prepare_write_cmd)
        prepare_thread.start()
        nodes = cycle(self.db_cluster.nodes)
        while prepare_thread.is_alive():
            # every 15 minutes, cycle thorough the nodes restart them
            sleep(900)
            node = next(nodes)
            if self.get_disk_usage(node) > 70:
                break
            self.log.info(f"Restarting node {node.name}.")
            node.stop_scylla(verify_down=True)
            node.start_scylla(verify_up=True)
            self.db_cluster.wait_for_nodes_up_and_normal(nodes=[node])
            self.log.info(f"Node {node.name} has restarted.")
        prepare_thread.join()

    def restart_at_97(self):
        restarted = False
        while not restarted:
            for node in self.db_cluster.nodes:
                disk_usage = self.get_disk_usage(node)
                if disk_usage >= 97:
                    self.log.info(f"Node {node.name} has reached 97% disk usage, restarting it.")
                    node.stop_scylla(verify_down=True)
                    node.start_scylla(verify_up=True)
                    restarted = True
                    break
            sleep(60)

    def wait_for_index(self, index_name: str, timeout: int, want_success: bool):
        node = self.db_cluster.nodes[0]
        try:
            wait_for_index_to_be_built(node, "keyspace1", index_name, timeout=timeout)
            if not want_success:
                self.error_event("Index creation should not finish.")
        except TimeoutError:
            if want_success:
                self.error_event("Index creation should have finished.")
            else:
                self.log.info(f"Index {index_name} creation timed out as expected")

        if want_success:
            with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
                verify_query_by_index_works(session, "keyspace1", "standard1", "C0")
                drop_index(session, "keyspace1", index_name)

    def wait_for_repair(self, repair_task: RepairTask, timeout: int, want_success: bool):
        try:
            task_final_status = repair_task.wait_and_get_final_status(timeout=timeout)
            if not want_success:
                self.error_event(f"Repair should not finish. Status: {task_final_status}")
        except WaitForTimeoutError:
            if want_success:
                self.error_event("Repair should have finished.")
            else:
                self.log.info(f"Repair task {repair_task.id} did not finish as expected.")

        if want_success:
            if task_final_status != TaskStatus.DONE:
                progress_full_string = repair_task.progress_string(
                    parse_table_res=False, is_verify_errorless_result=True).stdout
                if task_final_status != TaskStatus.ERROR_FINAL:
                    repair_task.stop()
                raise ScyllaManagerError(
                    f"Task: {repair_task.id} final status is: {task_final_status}.\nTask progress string: {progress_full_string}")
            self.log.info(f"Task: {repair_task.id} is done.")

        self.run_read_stress(want_success=want_success)

    def test_out_of_space_prevention(self):
        task_timeout = 3 * 3600  # 3 hours
        # Fill cluster to 90%, restart nodes during
        self.prepare_with_restarts()

        # wait until one node reaches 97%
        stress_thread = Thread(target=self.run_write_stress, kwargs={"want_success": False})
        stress_thread.start()
        while all(self.get_disk_usage(node) < 97 for node in self.db_cluster.nodes):
            sleep(60)

        # start a repair, should not finish
        mgr_cluster = self.db_cluster.get_cluster_manager()
        repair_task = mgr_cluster.create_repair_task()
        self.log.info(f"Repair task {repair_task.id} created.")
        self.wait_for_repair(repair_task, task_timeout, want_success=False)

        stress_thread.join()

        # scale out to disable out of space controller
        new_nodes = self.scale_out()

        # wait for repair to finish, should succeed
        self.wait_for_repair(repair_task, task_timeout, want_success=True)

        # reset for next part
        self.drop_new_keyspace()
        self.scale_in(new_nodes)

        # Write more data and restart one node at 97%
        stress_thread = Thread(target=self.run_write_stress, kwargs={"want_success": False})
        restart_thread = Thread(target=self.restart_at_97)
        stress_thread.start()
        restart_thread.start()

        stress_thread.join()
        try:
            restart_thread.join(timeout=600)
        except TimeoutError as e:
            self.error_event(f"Error when trying to restart node at 97%: {e}")

        # Check that the node that got to 98% does not have running compactions
        sleep(1200)
        threshold_node = max(self.db_cluster.nodes, key=self.get_disk_usage)
        if self.get_compactions(threshold_node, interval=600) != 0:
            self.error_event(f"Node {threshold_node.name} should not have running compactions on it")

        # scale out to disable out of space controller
        start_of_scale_out = time()
        new_nodes = self.scale_out()

        # Check that the node that got to 98% has running compactions
        if self.get_compactions(threshold_node, interval=int(time() - start_of_scale_out)) == 0:
            self.error_event(f"Node {threshold_node.name} should have had running compactions on it after scale out")

        # check that after scale out writes are accepted
        self.run_write_stress(want_success=True)
        return

        # reset for next part
        self.drop_new_keyspace()
        self.scale_in(new_nodes)

        # create a secondary index
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0], connect_timeout=300) as session:
            index_name = create_index(session, "keyspace1", "standard1", "C0")

        # wait for index to be built, should fail
        self.wait_for_index(index_name, task_timeout, want_success=False)

        # scale out to disable out of space controller
        self.scale_out()

        # wait for index to be built, should succeed
        self.wait_for_index(index_name, task_timeout, want_success=True)
