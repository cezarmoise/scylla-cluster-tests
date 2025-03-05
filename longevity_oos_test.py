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
from time import sleep
from longevity_test import LongevityTest
from sdcm.cluster import MAX_TIME_WAIT_FOR_NEW_NODE_UP, BaseNode
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent, CassandraStressLogEvent
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.tablets.common import wait_no_tablets_migration_running


class LongevityOutOfSpaceTest(LongevityTest):
    def write_data(self, idx: int):
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get("stress_cmd_w")

        if isinstance(stress_cmd, str):
            stress_cmd = [stress_cmd]
        stress_cmd = [cmd.replace("keyspace_name", f"keyspace{idx}") for cmd in stress_cmd]

        params = {"stress_cmd": stress_cmd, "round_robin": round_robin}
        stress_queue = []
        self._run_all_stress_cmds(stress_queue, params)

        for stress in stress_queue:
            self.verify_stress_thread(stress)

    def start_background_read(self):
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get("stress_cmd_r")

        params = {"stress_cmd": stress_cmd, "round_robin": round_robin}
        stress_queue = []
        self._run_all_stress_cmds(stress_queue, params)

    def scale_out(self) -> list[BaseNode]:
        instance_type = self.params.get("instance_type_db")
        added_nodes = self.db_cluster.add_nodes(count=self.db_cluster.racks_count,
                                                instance_type=instance_type, enable_auto_bootstrap=True, rack=None)
        self.monitors.reconfigure_scylla_monitoring()
        up_timeout = MAX_TIME_WAIT_FOR_NEW_NODE_UP
        with adaptive_timeout(Operations.NEW_NODE, node=self.db_cluster.data_nodes[0], timeout=up_timeout):
            self.db_cluster.wait_for_init(node_list=added_nodes, timeout=up_timeout, check_node_health=False)
        self.db_cluster.set_seeds()
        self.db_cluster.update_seed_provider()
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=added_nodes)
        return added_nodes

    def test_oos_scale_out(self):
        self.run_prepare_write_cmd()
        self.start_background_read()

        sleep(600)
        with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60), \
                EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressLogEvent, extra_time_to_expiration=60):
            self.write_data(1)

        sleep(600)
        self.scale_out()
        for node in self.db_cluster.nodes:
            wait_no_tablets_migration_running(node)

        sleep(600)
        self.write_data(2)
