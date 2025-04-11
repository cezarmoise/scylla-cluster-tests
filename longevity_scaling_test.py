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
import time
from longevity_test import LongevityTest
from sdcm.cluster import MAX_TIME_WAIT_FOR_DECOMMISSION, MAX_TIME_WAIT_FOR_NEW_NODE_UP, BaseNode
from sdcm.cluster_aws import AWSNode
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent
from sdcm.stress_thread import CassandraStressThread
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.common import ParallelObject
from sdcm.utils.tablets.common import wait_no_tablets_migration_running

instance_size_map = {"i4i.large": 1, "i4i.xlarge": 2, "i4i.2xlarge": 4, "i4i.4xlarge": 8, "i4i.8xlarge": 16}


def get_node_disk_usage(node: BaseNode) -> int:
    """Returns disk usage data for a node"""
    result = node.remoter.run("df -h -BG --output=pcent /var/lib/scylla | sed 1d | sed 's/%//'")
    return int(result.stdout.strip())


def get_instance_type(node: AWSNode):
    return node._instance.instance_type


def upgrade_cluster(node_types, min_nodes) -> tuple[str, list]:
    """Determine what instances to add and what nodes to remove"""

    if "i4i.large" not in node_types:
        return "i4i.large", []

    sorted_nodes = sorted(node_types, key=lambda x: instance_size_map[x])
    increasing_sequence = [sorted_nodes[0]]
    for i in range(1, len(sorted_nodes) - min_nodes + 1):
        if instance_size_map[sorted_nodes[i]] == instance_size_map[increasing_sequence[-1]] * 2:
            increasing_sequence.append(sorted_nodes[i])
        else:
            break

    combined_size = sum(instance_size_map[inst] for inst in increasing_sequence) + 1
    upgrade_target = [k for k, v in instance_size_map.items() if v == combined_size]

    if upgrade_target:
        return upgrade_target[0], increasing_sequence


class LongevityScalingTest(LongevityTest):
    def write_data(self, stress_queue: list[CassandraStressThread], idx: int):
        keyspace_num = self.params.get("keyspace_num")
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get("stress_cmd")

        if isinstance(stress_cmd, str):
            stress_cmd = [stress_cmd]
        stress_cmd = [cmd.replace("keyspace_name", f"keyspace_write_{idx}") for cmd in stress_cmd]

        params = {"keyspace_num": keyspace_num, "stress_cmd": stress_cmd, "round_robin": round_robin}
        self._run_all_stress_cmds(stress_queue, params)

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

    def get_nodes_to_remove(self, live_nodes, instance_types_to_remove):
        nodes_to_remove = []
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
        self.run_prepare_write_cmd()
        stress_queue = []
        idx = 0
        self.write_data(stress_queue, idx)

        min_nodes_per_rack = len(self.db_cluster.nodes) // self.db_cluster.racks_count
        live_nodes = self.db_cluster.nodes[:]

        while not all("i4i.8xlarge" == get_instance_type(node) for node in live_nodes):
            usages = {node: get_node_disk_usage(node) for node in live_nodes}
            self.log.info("SCALING CLUSTER: " + ", ".join(f"{k.name}: {v}%" for k, v in usages.items()))

            if any(u > 98 for u in usages.values()):
                self.log.error("SCALING CLUSTER: stop test when one node reaches 98%")
                break

            if any(u > 90 for u in usages.values()):
                if any(u < 70 for u in usages.values()):
                    self.log.error("SCALING CLUSTER: stop test due to balancing issues")
                    break
                idx += 1
                node_types_in_rack = [get_instance_type(node) for node in live_nodes if node.rack == live_nodes[0].rack]
                instance_type_to_add, instance_types_to_remove = upgrade_cluster(
                    node_types_in_rack, min_nodes=min_nodes_per_rack)

                self.log.info(f"SCALING CLUSTER: +{instance_type_to_add} -{instance_types_to_remove}")

                # add new nodes
                live_nodes.extend(self.scale_out(instance_type_to_add))

                # wait for tablet migration to finish
                for node in live_nodes:
                    wait_no_tablets_migration_running(node)

                # remove nodes if necessary
                for instance_type_to_remove in instance_types_to_remove:
                    # remove one instance type at a time so racks don't differ by more than 1 node
                    nodes_to_remove = self.get_nodes_to_remove(live_nodes, [instance_type_to_remove])
                    self.scale_in(nodes_to_remove)
                    live_nodes = [node for node in live_nodes if node not in nodes_to_remove]

                # wait for tablet migration to finish
                for node in live_nodes:
                    wait_no_tablets_migration_running(node)

                # start new stress writes to keep load the same
                self.write_data(stress_queue, idx)
                self.log.info(f"SCALING CLUSTER: started stress #{idx}")

            time.sleep(60)
            # sleep more if not close to 90%
            if all(u < 85 for u in usages.values()):
                time.sleep(300)
            if all(u < 80 for u in usages.values()):
                time.sleep(300)

        with EventsSeverityChangerFilter(
            new_severity=Severity.NORMAL,  # killing stress creates Critical error
            event_class=CassandraStressEvent,
            extra_time_to_expiration=60,
        ):
            self.loaders.kill_stress_thread()
