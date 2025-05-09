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
import re
import time
from longevity_test import LongevityTest
from sdcm.argus_results import timer_results_to_argus
from sdcm.cluster import MAX_TIME_WAIT_FOR_DECOMMISSION, MAX_TIME_WAIT_FOR_NEW_NODE_UP, BaseNode
from sdcm.cluster_aws import AWSNode
from sdcm.mgmt.operations import ManagerTestFunctionsMixIn
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent
from sdcm.sct_events.system import TestFrameworkEvent
from sdcm.stress_thread import CassandraStressThread
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.common import ParallelObject
from sdcm.utils.tablets.common import wait_no_tablets_migration_running

INSTANCE_SIZE_MAP = {"i4i.large": 1, "i4i.xlarge": 2, "i4i.2xlarge": 4, "i4i.4xlarge": 8, "i4i.8xlarge": 16}


def get_node_disk_usage(node: BaseNode) -> int:
    """Returns disk usage data for a node"""
    result = node.remoter.run("df -h -BG --output=pcent /var/lib/scylla | sed 1d | sed 's/%//'")
    return int(result.stdout.strip())


def get_instance_type(node: AWSNode) -> str:
    return node._instance.instance_type


def upgrade_cluster(node_types, min_nodes) -> tuple[str, list[str]]:
    """
    Determine what instances to add and what nodes to remove

    Tries to increase the total "size" of the cluster by 1
    by adding a bigger instance type and removing smaller ones
    """

    if "i4i.large" not in node_types:
        return "i4i.large", []

    sorted_nodes = sorted(node_types, key=lambda x: INSTANCE_SIZE_MAP[x])
    increasing_sequence = [sorted_nodes[0]]
    for i in range(1, len(sorted_nodes) - min_nodes + 1):
        if INSTANCE_SIZE_MAP[sorted_nodes[i]] == INSTANCE_SIZE_MAP[increasing_sequence[-1]] * 2:
            increasing_sequence.append(sorted_nodes[i])
        else:
            break

    combined_size = sum(INSTANCE_SIZE_MAP[inst] for inst in increasing_sequence) + 1
    upgrade_target = [k for k, v in INSTANCE_SIZE_MAP.items() if v == combined_size]

    if upgrade_target:
        return upgrade_target[0], increasing_sequence


def multiply_rate(stress_cmd: str, rate_multiplier: int) -> str:
    """Multiply the rate in the stress command by the rate multiplier"""
    rate = int(re.search(r"fixed=(\d+)/s", stress_cmd).group(1))
    return re.sub(r"fixed=\d+/s", f"fixed={rate * rate_multiplier}/s", stress_cmd)


class LongevityScalingTest(LongevityTest, ManagerTestFunctionsMixIn):
    def run_stress(self, stress_queue: list[CassandraStressThread], cmd_name: str):
        """
        Run the stress command with the given name
        Replace the keyspace name with a unique one
        Multiply the rate based the cluster topology
        """
        keyspace_num = self.params.get("keyspace_num")
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get(cmd_name)

        rate_multiplier = sum(INSTANCE_SIZE_MAP[get_instance_type(inst)]
                              for inst in self.db_cluster.nodes) // self.db_cluster.racks_count

        if isinstance(stress_cmd, str):
            stress_cmd = [stress_cmd]
        stress_cmd = [cmd.replace("keyspace_name", f"keyspace_write_{rate_multiplier}") for cmd in stress_cmd]

        stress_cmd = [multiply_rate(cmd, rate_multiplier) for cmd in stress_cmd]

        params = {"keyspace_num": keyspace_num, "stress_cmd": stress_cmd, "round_robin": round_robin}
        self._run_all_stress_cmds(stress_queue, params)

    def start_background_load(self, stress_queue: list[CassandraStressThread]):
        self.run_stress(stress_queue, "stress_cmd")

    def start_fill_load(self, stress_queue: list[CassandraStressThread]):
        self.run_stress(stress_queue, "stress_cmd_w")

    def scale_out(self, instance_type: str) -> list[BaseNode]:
        self.log.info(f"SCALING CLUSTER: started adding {self.db_cluster.racks_count} x {instance_type}")
        added_nodes = self.db_cluster.add_nodes(count=self.db_cluster.racks_count,
                                                instance_type=instance_type, enable_auto_bootstrap=True, rack=None)
        self.monitors.reconfigure_scylla_monitoring()
        up_timeout = MAX_TIME_WAIT_FOR_NEW_NODE_UP
        with adaptive_timeout(Operations.NEW_NODE, node=self.db_cluster.data_nodes[0], timeout=up_timeout):
            self.db_cluster.wait_for_init(node_list=added_nodes, timeout=up_timeout, check_node_health=False)
        self.db_cluster.set_seeds()
        self.db_cluster.update_seed_provider()
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=added_nodes)
        self.log.info(f"SCALING CLUSTER: done adding {self.db_cluster.racks_count} x {instance_type}")
        return added_nodes

    def scale_in(self, nodes: list[BaseNode]):
        num_workers = None if (self.db_cluster.parallel_node_operations and nodes[0].raft.is_enabled) else 1
        parallel_obj = ParallelObject(objects=nodes, timeout=MAX_TIME_WAIT_FOR_DECOMMISSION, num_workers=num_workers)

        def _decommission(node: BaseNode):
            try:
                self.log.info(f'SCALING CLUSTER: started decommissioning a node {node}')
                self.db_cluster.decommission(node)
                self.log.info(f'SCALING CLUSTER: done decommissioning a node {node}')
            except Exception as exc:  # pylint: disable=broad-except  # noqa: BLE001
                self.log.error(f'SCALING CLUSTER: failed decommissioning a node {node}', exc_info=exc)
                raise
        parallel_obj.run(_decommission, ignore_exceptions=False, unpack_objects=True)
        self.monitors.reconfigure_scylla_monitoring()
        self.log.info(f"SCALING CLUSTER: done decommissioning {len(nodes)} nodes")

    def get_nodes_to_remove(self, live_nodes: list[BaseNode], instance_types_to_remove: list[str]) -> list[BaseNode]:
        """"
        Get a list of nodes to remove from each rack by their instance type
        """
        nodes_to_remove: list[BaseNode] = []
        for rack in self.db_cluster.racks:
            for instance_type in instance_types_to_remove:
                for node in live_nodes:
                    if get_instance_type(node) == instance_type and node.rack == rack:
                        nodes_to_remove.append(node)
                        break
                else:
                    continue
        return nodes_to_remove

    def test_cluster_scaling(self):
        """
        Run a test that scales the cluster up
        The test starts with a cluster of i4i.large nodes
        and scales it down to i4i.8xlarge nodes
        The test runs a background mixed load
        and a fill write load between topology changes
        """
        self.run_prepare_write_cmd()
        background_stress_queue: list[CassandraStressThread] = []
        fill_stress_queue: list[CassandraStressThread] = []

        self.start_background_load(background_stress_queue)
        self.start_fill_load(fill_stress_queue)

        min_nodes_per_rack = len(self.db_cluster.nodes) // self.db_cluster.racks_count

        while not all("i4i.8xlarge" == get_instance_type(node) for node in self.db_cluster.nodes):
            usages = {node: get_node_disk_usage(node) for node in self.db_cluster.nodes}
            self.log.info("SCALING CLUSTER: " + ", ".join(f"{k.name}: {v}%" for k, v in usages.items()))

            if any(u > 98 for u in usages.values()):
                self.log.error("SCALING CLUSTER: stop test when one node reaches 98%")
                TestFrameworkEvent(source="longevity_scaling_test",
                                   message="stop test when one node reaches 98%", severity=Severity.CRITICAL).publish()
                break

            if any(u > 90 for u in usages.values()):
                if any(u < 70 for u in usages.values()):
                    self.log.error("SCALING CLUSTER: stop test due to balancing issues")
                    TestFrameworkEvent(source="longevity_scaling_test",
                                       message="stop test due to balancing issues", severity=Severity.CRITICAL).publish()
                    break

                # stop fill writes to avoid overloading the cluster during topology changes
                with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
                    for stress in fill_stress_queue:
                        stress.kill()

                node_types_in_rack = [get_instance_type(
                    node) for node in list(self.db_cluster.nodes_by_racks_idx_and_regions().values())[0]]
                instance_type_to_add, instance_types_to_remove = upgrade_cluster(
                    node_types_in_rack, min_nodes=min_nodes_per_rack)

                self.log.info(f"SCALING CLUSTER: +{instance_type_to_add} -{instance_types_to_remove}")

                # add new nodes
                self.scale_out(instance_type_to_add)

                # wait for tablet migration to finish
                for node in self.db_cluster.nodes:
                    wait_no_tablets_migration_running(node)

                # remove nodes if necessary
                if instance_types_to_remove:
                    nodes_to_remove = self.get_nodes_to_remove(self.db_cluster.nodes, instance_types_to_remove)
                    self.scale_in(nodes_to_remove)

                # replace background load with higher rate
                with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
                    for stress in background_stress_queue:
                        stress.kill()
                self.start_background_load(background_stress_queue)
                self.start_fill_load(fill_stress_queue)

            time.sleep(60)
            # sleep more if not close to 90%
            if all(u < 85 for u in usages.values()):
                time.sleep(300)
            if all(u < 80 for u in usages.values()):
                time.sleep(300)

        # killing stress creates Critical error
        with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
            self.loaders.kill_stress_thread()

    def test_scaling_single_step(self):
        """
        Test a single step of scaling

        The test looks at `instance_type_db` and fills the cluster with smaller instance types
        Then it scales out with a bigger instance type and removes the smaller instance types
        """
        background_stress_queue: list[CassandraStressThread] = []
        fill_stress_queue: list[CassandraStressThread] = []
        argus_client = self.test_config.argus_client()
        self.db_cluster.add_nemesis(nemesis=self.get_nemesis_class(), tester_obj=self)

        # add instances smaller than the initial one
        initial_instance_size = INSTANCE_SIZE_MAP[self.params.get('instance_type_db')]
        for instance_type, size in INSTANCE_SIZE_MAP.items():
            if size < initial_instance_size:
                self.log.info(f"SCALING CLUSTER: +{instance_type}")
                self.scale_out(instance_type)

        self.log.info("SCALING CLUSTER: starting fill load")
        self.start_fill_load(fill_stress_queue)

        # stop load at 90%
        while True:
            usages = {node: get_node_disk_usage(node) for node in self.db_cluster.nodes}
            if any(u > 90 for u in usages.values()):
                self.log.info("SCALING CLUSTER: killing fill load")
                with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
                    for stress in fill_stress_queue:
                        stress.kill()
                break
            time.sleep(60)

        # sleep a little to let the cluster stabilize
        time.sleep(900)

        # start nemesis
        self.db_cluster.start_nemesis()

        # start background load
        self.log.info("SCALING CLUSTER: start background load")
        self.start_background_load(background_stress_queue)

        current_instance_types = [get_instance_type(node)
                                  for node in list(self.db_cluster.nodes_by_racks_idx_and_regions().values())[0]]
        instance_type_to_add, instance_types_to_remove = upgrade_cluster(current_instance_types, min_nodes=1)
        self.log.info(f"SCALING CLUSTER: {current_instance_types=}")
        self.log.info(f"SCALING CLUSTER: {instance_type_to_add=}")
        self.log.info(f"SCALING CLUSTER: {instance_types_to_remove=}")
        with timer_results_to_argus(f"Scaling: +{instance_type_to_add} -{' - '.join(instance_types_to_remove)}", argus_client):
            # scale out with bigger instance type
            self.scale_out(instance_type_to_add)

            # wait for tablet migration to finish
            for node in self.db_cluster.nodes:
                wait_no_tablets_migration_running(node)

            if instance_types_to_remove:
                # remove one instance type at a time so racks don't differ by more than 1 node
                # remove biggest instance type first
                nodes_to_remove = self.get_nodes_to_remove(self.db_cluster.nodes, instance_types_to_remove)
                self.scale_in(nodes_to_remove)

        # killing background load creates to end the test
        with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
            self.loaders.kill_stress_thread()
