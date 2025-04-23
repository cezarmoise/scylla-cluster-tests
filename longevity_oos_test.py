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
from contextlib import ExitStack, contextmanager
from time import sleep
from longevity_test import LongevityTest
from sdcm.cluster import MAX_TIME_WAIT_FOR_NEW_NODE_UP, BaseNode
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent, CassandraStressLogEvent
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.context_managers import DbNodeLogger
from sdcm.utils.nemesis_utils.indexes import verify_query_by_index_works, wait_for_index_to_be_built
from sdcm.utils.tablets.common import wait_no_tablets_migration_running


@contextmanager
def ignore_stress_errors():
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
    idx = 0  # counter for keyspace names

    def write_data(self):
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get("stress_cmd_w")

        if isinstance(stress_cmd, str):
            stress_cmd = [stress_cmd]
        stress_cmd = [cmd.replace("keyspace_name", f"keyspace{self.idx}") for cmd in stress_cmd]
        self.idx += 1

        params = {"stress_cmd": stress_cmd, "round_robin": round_robin}
        stress_queue = []
        self._run_all_stress_cmds(stress_queue, params)

        for stress in stress_queue:
            self.verify_stress_thread(stress)

    def create_secondary_index(self, column: str):
        ks = "keyspace1"
        cf = "standard1"
        index_name = f"{cf}_{column}_oos"
        node = self.db_cluster.nodes[0]
        with DbNodeLogger(self.db_cluster.nodes, "create index", target_node=node, additional_info=f"on {ks}.{cf}.{column}"):
            with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
                session.execute(f'CREATE INDEX {index_name} ON {ks}.{cf}("{column}")', timeout=600)

        with adaptive_timeout(operation=Operations.CREATE_INDEX, node=node, timeout=14400) as timeout:
            wait_for_index_to_be_built(node, ks, index_name, timeout=timeout * 2)
        verify_query_by_index_works(session, ks, cf, column)

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

    def test_oos_write_scale_out(self):
        self.run_prepare_write_cmd()
        self.start_background_read()

        sleep(600)
        with ignore_stress_errors():
            self.write_data()

        sleep(600)
        self.scale_out()
        for node in self.db_cluster.nodes:
            wait_no_tablets_migration_running(node)

        sleep(600)
        self.write_data()

    def test_oos_si_scale_out(self):
        self.run_prepare_write_cmd()
        # self.start_background_read()

        sleep(600)
        # create index
        ks = "keyspace1"
        cf = "standard1"
        column = "C0"
        index_name = f"{cf}_{column}_oos"
        node = self.db_cluster.nodes[0]
        with DbNodeLogger(self.db_cluster.nodes, "create index", target_node=node, additional_info=f"on {ks}.{cf}.{column}"):
            with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
                session.execute(f'CREATE INDEX {index_name} ON {ks}.{cf}("{column}")', timeout=600)

        try:
            with adaptive_timeout(operation=Operations.CREATE_INDEX, node=node, timeout=3600) as timeout:
                wait_for_index_to_be_built(node, ks, index_name, timeout=timeout * 2)
        except TimeoutError:
            # index creation will not finish due to lack of space
            pass

        sleep(600)
        self.scale_out()
        for node in self.db_cluster.nodes:
            wait_no_tablets_migration_running(node)

        sleep(600)
        with adaptive_timeout(operation=Operations.CREATE_INDEX, node=node, timeout=3600) as timeout:
            wait_for_index_to_be_built(node, ks, index_name, timeout=timeout * 2)
        verify_query_by_index_works(session, ks, cf, column)
