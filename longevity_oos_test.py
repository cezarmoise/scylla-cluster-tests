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
from cassandra.query import SimpleStatement
from contextlib import ExitStack, contextmanager
from time import sleep
from longevity_test import LongevityTest
from sdcm.cluster import MAX_TIME_WAIT_FOR_NEW_NODE_UP
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent, CassandraStressLogEvent
from sdcm.sct_events.system import InfoEvent, TestFrameworkEvent
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.utils.nemesis_utils.indexes import create_index, get_column_names, get_random_column_name, verify_query_by_index_works, wait_for_index_to_be_built, wait_for_view_to_be_built
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

    def start_background_read(self):
        round_robin = self.params.get("round_robin")
        stress_cmd = self.params.get("stress_cmd_r")

        params = {"stress_cmd": stress_cmd, "round_robin": round_robin}
        stress_queue = []
        self._run_all_stress_cmds(stress_queue, params)

    def scale_out(self):
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
        for node in self.db_cluster.nodes:
            wait_no_tablets_migration_running(node)

    def test_oos_write_scale_out(self):
        self.run_prepare_write_cmd()

        sleep(600)
        with ignore_stress_errors():
            self.write_data()

        sleep(600)
        self.scale_out()

        sleep(600)
        self.write_data()

    def test_oos_si_scale_out(self):
        self.run_prepare_write_cmd()

        sleep(600)
        # create index
        ks = "keyspace1"
        cf = "standard1"
        column = "C0"
        node = self.db_cluster.nodes[0]
        timeout = 2 * 3600

        with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
            index_name = create_index(session, ks, cf, column)

        try:
            wait_for_index_to_be_built(node, ks, index_name, timeout=timeout)
            TestFrameworkEvent(message="Index creation should not finish.", severity=Severity.CRITICAL).publish()
        except TimeoutError:
            self.log.info(f"Index {index_name} creation timed out as expected")

        sleep(600)
        self.scale_out()

        sleep(600)
        wait_for_index_to_be_built(node, ks, index_name, timeout=timeout)

        sleep(600)
        with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
            verify_query_by_index_works(session, ks, cf, column)

        sleep(600)

    def test_oos_mv_scale_out(self):
        self.run_prepare_write_cmd()

        sleep(600)
        # create index
        ks = "keyspace1"
        cf = "standard1"
        view_name = f'{cf}_view'
        node = self.db_cluster.nodes[0]
        timeout = 2 * 3600

        with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
            primary_key_columns = get_column_names(session=session, ks=ks, cf=cf, is_primary_key=True)
            column = get_random_column_name(session=session, ks=ks,
                                            cf=cf, filter_out_collections=True, filter_out_static_columns=True)
            self.create_materialized_view(ks, cf, view_name, [column], primary_key_columns, session, mv_columns=[
                column] + primary_key_columns)

        try:
            wait_for_view_to_be_built(node, ks, view_name, timeout=timeout)
            TestFrameworkEvent(message="Materialized view creation should not finish.",
                               severity=Severity.CRITICAL).publish()
        except TimeoutError:
            self.log.info(f"Materialized view {view_name} creation timed out as expected")

        sleep(600)
        self.scale_out()

        sleep(600)
        wait_for_view_to_be_built(node, ks, view_name, timeout=timeout)

        sleep(600)
        try:
            with self.db_cluster.cql_connection_patient(node, connect_timeout=300) as session:
                query = SimpleStatement(f'SELECT * FROM {ks}.{view_name} limit 1', fetch_size=10)
                self.log.debug("Verifying query by index works: %s", query)
                result = session.execute(query)
        except Exception as exc:  # noqa: BLE001
            InfoEvent(message=f"Materialized view {ks}.{view_name} does not work in query: {query}. Reason: {exc}",
                      severity=Severity.ERROR).publish()
        if len(list(result)) == 0:
            InfoEvent(message=f"Materialized view {ks}.{view_name} does not work. No rows returned for query {query}",
                      severity=Severity.ERROR).publish()
