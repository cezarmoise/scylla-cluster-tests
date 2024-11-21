import time

from argus.client.generic_result import Status
from full_storage_utilization_test import FullStorageUtilizationTest
from sdcm.argus_results import DiskUsageResult, submit_results_to_argus
from sdcm.cluster import BaseNode
from sdcm.utils.tablets.common import wait_for_tablets_balanced
from sdcm.utils.replication_strategy_utils import NetworkTopologyReplicationStrategy


class FullStorageUtilizationTest2(FullStorageUtilizationTest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_removal_action = self.params.get('data_removal_action')
        self.scale_out_instance_type = self.params.get('scale_out_instance_type')
        self.scale_out_dc_idx = self.params.get("scale_out_dc_idx")
        self.keyspaces = []

    def get_total_free_space(self):
        free = 0
        for node in self.db_cluster.nodes:
            info = self.get_disk_info(node)
            free += info["available"]

        return free

    def get_keyspaces(self, prefix: str):
        rows = self.execute_cql(f"SELECT keyspace_name FROM system_schema.keyspaces")
        return [row.keyspace_name for row in rows if row.keyspace_name.startswith(prefix)]
    
    def execute_cql(self, query):
        node = self.db_cluster.nodes[0]
        self.log.info(f"Executing {query}")
        with self.db_cluster.cql_connection_patient(node) as session:
            results = session.execute(query)

        return results
    
    def remove_data_from_cluster(self, keyspace: str):
        table = "standard1"
        match self.data_removal_action:
            case "drop":
                self.execute_cql(f"DROP TABLE {keyspace}.{table}")
            case "truncate":
                self.execute_cql(f"TRUNCATE TABLE {keyspace}.{table}")
            case "expire":
                self.log.info(f"Sleep 1h to let data expire")
                time.sleep(3600)
            case _:
                raise ValueError(f"data_removal_action={self.data_removal_action} is not supported!")

    def scale_out(self):
        self.start_throttle_rw()
        self.log.info("Started adding a new node")
        start_time = time.time()
        self.add_new_node()
        duration = time.time() - start_time
        self.log.info(f"Adding a node finished with time: {duration}")

    def add_new_node(self):
        status = self.db_cluster.get_nodetool_status()
        system_keyspaces = ["system_distributed", "system_traces"]
        # auth-v2 is used when consistent topology is enabled
        if not self.db_cluster.nodes[0].raft.is_consistent_topology_changes_enabled:
            system_keyspaces.insert(0, "system_auth")
        self.reconfigure_keyspaces_to_use_network_topology_strategy(
            keyspaces=system_keyspaces,
            replication_factors={dc: len(status[dc].keys()) for dc in status}
        )
        new_nodes = self.db_cluster.add_nodes(count=self.add_node_cnt, enable_auto_bootstrap=True, dc_idx=self.scale_out_dc_idx, instance_type=self.scale_out_instance_type)
        self.db_cluster.wait_for_init(node_list=new_nodes)
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=new_nodes)
        total_nodes_in_cluster = len(self.db_cluster.nodes)
        self.log.info(f"New node added, total nodes in cluster: {total_nodes_in_cluster}")
        if self.scale_out_dc_idx not in [0, None]:
            self.extend_to_new_dc(new_nodes, system_keyspaces)
        self.monitors.reconfigure_scylla_monitoring()
        wait_for_tablets_balanced(self.db_cluster.nodes[0])

    def extend_to_new_dc(self, new_nodes: list[BaseNode], system_keyspaces: list[str]):
        status = self.db_cluster.get_nodetool_status()
        # reconfigure keyspaces to use NetworkTopologyStrategy
        self.reconfigure_keyspaces_to_use_network_topology_strategy(
            keyspaces=system_keyspaces + self.keyspaces,
            replication_factors={dc: len(status[dc].keys()) for dc in status}
        )

        self.log.info("Running repair on new nodes")
        for node in new_nodes:
            node.run_nodetool(sub_cmd=f"rebuild -- {list(status.keys())[0]}", publish_event=True)

        self.log.info("Running repair on all nodes")
        for node in self.db_cluster.nodes:
            node.run_nodetool(sub_cmd="repair -pr", publish_event=True)

    def reconfigure_keyspaces_to_use_network_topology_strategy(self, keyspaces: list[str], replication_factors: dict[str, int]) -> None:
        self.log.info("Reconfiguring keyspace Replication Strategy")
        for keyspace in keyspaces:
            cql = f"ALTER KEYSPACE {keyspace} WITH replication = {NetworkTopologyReplicationStrategy(**replication_factors)}"
            self.execute_cql(cql)
        self.log.info("Replication Strategies for {} reconfigured".format(keyspaces))

    def alter_table_with_ttl(self, keyspace: str, table: str = "standard1"):
        ttl = 4 * 3600 # 4 hours
        self.execute_cql(f"ALTER TABLE {keyspace}.{table} WITH default_time_to_live = {ttl} AND gc_grace_seconds = 300")

    def run_stress_queue(self, dataset_size: int, ks_name: str, ks_num: int):
        stress_cmd = self.prepare_dataset_layout(dataset_size)
        stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, keyspace_name=ks_name, stress_num=1, keyspace_num=ks_num)
        self.verify_stress_thread(cs_thread_pool=stress_queue)
        self.get_stress_results(queue=stress_queue)

    def insert_data(self, dataset_size: int, ks_name: str, ks_num: int):
        if self.data_removal_action == "expire":
            # write half the data, then alter the table with a TTL
            first_half = dataset_size // 2
            if first_half != 0:
                dataset_size -= first_half
                self.run_stress_queue(first_half, ks_name, ks_num)
                self.alter_table_with_ttl(ks_name)

        self.run_stress_queue(dataset_size, ks_name, ks_num)

    def run_stress_until_target(self, target_used_size, target_usage):
        current_usage, current_used = self.get_max_disk_usage()
        
        big_chunk = int((target_used_size - current_used) * 0.1)
        while current_used < target_used_size and current_usage < target_usage:
            # Write smaller dataset near the threshold (15% or 30GB of the target)
            smaller_dataset = (((target_used_size - current_used) < 30) or ((target_usage - current_usage) <= 15))

            # Use smaller chunks (half the remaining space) near threshold, otherwise use 10% of remaining space
            small_chunk = min(int((target_used_size - current_used) / 2), 1)
            dataset_size = small_chunk if smaller_dataset else big_chunk
            num = len(self.keyspaces) + 1
            ks_name = f"keyspace_{'small' if smaller_dataset else 'large'}{num}"
            self.keyspaces.append(ks_name)
            self.log.info(f"Writing chunk of size: {dataset_size} GB")
            self.insert_data(dataset_size, ks_name, num)

            self.db_cluster.flush_all_nodes()

            current_usage, current_used = self.get_max_disk_usage()
            self.log.info(f"Current max disk usage after writing to {ks_name}: {current_usage}% ({current_used} GB / {target_used_size} GB)")

    def get_disk_usage_data(self):
        """Returns disk usage data for all nodes and cluster totals"""
        data = {}
        total = 0
        used = 0 
        available = 0
        usage_percents = []

        for idx, node in enumerate(self.db_cluster.nodes, 1):
            info = self.get_disk_info(node)
            data[f"node_{idx}"] = {
                'Total': info['total'],
                'Used': info['used'],
                'Available': info['available'],
                'Usage %': info['used_percent']
            }
            
            total += info['total']
            used += info['used']
            available += info['available']
            usage_percents.append(info['used_percent'])

        data["cluster"] = {
            'Total': total,
            'Used': used,
            'Available': available,
            'Usage %': sum(usage_percents) / len(usage_percents)
        }

        return data

    def log_disk_usage(self):
        data = self.get_disk_usage_data()
        
        headers = f"{'Node':<8} {'Total GB':<12} {'Used GB':<12} {'Avail GB':<12} {'Used %':<8}"
        self.log.info(headers)

        for idx in range(1, len(self.db_cluster.nodes) + 1):
            node_data = data[f"node_{idx}"]
            row = f"{idx:<8} {node_data['Total']:<12} {node_data['Used']:<12} {node_data['Available']:<12} {node_data['Usage %']:.1f}%"
            self.log.info(row)

        cluster_data = data["cluster"]
        total = f"{'Cluster':<8} {cluster_data['Total']:<12} {cluster_data['Used']:<12} {cluster_data['Available']:<12} {cluster_data['Usage %']:.1f}%"
        self.log.info(total)

    def disk_usage_to_argus(self, label: str):
        data = self.get_disk_usage_data()
        
        for idx in range(1, len(self.db_cluster.nodes) + 1):
            node_data = data[f"node_{idx}"]
            row = f"{f'Node {idx}':<8} [{label}]"
            self.report_to_argus(node_data, row)

        row = f"{'Cluster':<8} [{label}]"
        self.report_to_argus(data["cluster"], row)

    def report_to_argus(self, data: dict, row: str):
        argus_client = self.test_config.argus_client()
        data_table = DiskUsageResult()
        for key, value in data.items():
            data_table.add_result(column=key, row=row, value=value, status=Status.UNSET)
        submit_results_to_argus(argus_client, data_table)

    def test_data_removal_compaction(self):
        """
        3 nodes cluster, RF=3.
        Write data until 90% disk usage is reached.
        Sleep for 60 minutes.
        Drop some data and verify space was reclaimed
        """
        self.run_stress(self.softlimit, sleep_time=self.sleep_time_fill_disk)
        self.run_stress(self.hardlimit, sleep_time=self.sleep_time_fill_disk)
        self.disk_usage_to_argus(label="After data insertion")

        free_before = self.get_total_free_space()

        # remove data from 2 large and 2 small keyspaces
        for keyspace in self.get_keyspaces(prefix="keyspace_large")[:2]:
            self.remove_data_from_cluster(keyspace)
            time.sleep(600)
        for keyspace in self.get_keyspaces(prefix="keyspace_small")[:2]:
            self.remove_data_from_cluster(keyspace)
            time.sleep(600)

        # sleep to let compaction happen
        time.sleep(1800)
        self.log_disk_usage() 
        self.disk_usage_to_argus(label="After data removal")

        free_after = self.get_total_free_space()
        assert free_after > free_before, "space was not freed after dropping data"

    def test_scale_out(self):
        """
        3 nodes cluster, RF=3.
        Write data until 90% disk usage is reached.
        Sleep for 60 minutes.
        Add a new node
        """
        self.run_stress(self.softlimit, sleep_time=self.sleep_time_fill_disk)
        self.run_stress(self.hardlimit, sleep_time=self.sleep_time_fill_disk)

        self.scale_out()
        self.log_disk_usage() 

        time.sleep(1800)
        self.log_disk_usage() 
