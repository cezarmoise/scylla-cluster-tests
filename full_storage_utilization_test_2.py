import random
import time

from full_storage_utilization_test import FullStorageUtilizationTest
from sdcm.cluster import BaseNode
from sdcm.utils.tablets.common import wait_for_tablets_balanced
from sdcm.utils.replication_strategy_utils import NetworkTopologyReplicationStrategy


class FullStorageUtilizationTest2(FullStorageUtilizationTest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_removal_action = self.params.get('data_removal_action')
        self.scale_out_instance_type = self.params.get('scale_out_instance_type')
        self.scale_out_dc_idx = int(self.params.get("scale_out_dc_idx")) or 0
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
        new_nodes = self.db_cluster.add_nodes(count=self.add_node_cnt, enable_auto_bootstrap=True, dc_idx=self.scale_out_dc_idx, instance_type=self.scale_out_instance_type)
        self.db_cluster.wait_for_init(node_list=new_nodes)
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=new_nodes)
        total_nodes_in_cluster = len(self.db_cluster.nodes)
        self.log.info(f"New node added, total nodes in cluster: {total_nodes_in_cluster}")
        if self.scale_out_dc_idx not in [0, None]:
            self.extend_to_new_dc(new_nodes)
        self.monitors.reconfigure_scylla_monitoring()
        wait_for_tablets_balanced(self.db_cluster.nodes[0])

    def extend_to_new_dc(self, new_nodes: list[BaseNode]):
        # reconfigure system keyspaces to use NetworkTopologyStrategy
        status = self.db_cluster.get_nodetool_status()
        self.reconfigure_keyspaces_to_use_network_topology_strategy(
            keyspaces=["system_distributed", "system_traces"],
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

    def create_ks_with_ttl(self, keyspace: str):
        ttl = random.randint(4, 12) * 3600
        self.execute_cql(f"""CREATE KEYSPACE {keyspace} 
                         WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 3}
                         AND default_time_to_live = {ttl};""")

    def run_stress_until_target(self, target_used_size, target_usage):
        current_usage, current_used = self.get_max_disk_usage()
        
        space_needed = target_used_size - current_used
        # Calculate chunk size as 10% of space needed
        chunk_size = int(space_needed * 0.1)
        while current_used < target_used_size and current_usage < target_usage:
            # Write smaller dataset near the threshold (15% or 30GB of the target)
            smaller_dataset = (((target_used_size - current_used) < 30) or ((target_usage - current_usage) <= 15))

            # Use 1GB chunks near threshold, otherwise use 10% of remaining space
            num = len(self.keyspaces) + 1
            dataset_size = 1 if smaller_dataset else chunk_size
            ks_name = f"keyspace_{'small' if smaller_dataset else 'large'}{num}"
            self.log.info(f"Writing chunk of size: {dataset_size} GB")
            stress_cmd = self.prepare_dataset_layout(dataset_size)
            if self.data_removal_action == "expire":
                self.create_ks_with_ttl(ks_name)
            stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, keyspace_name=f"{ks_name}", stress_num=1, keyspace_num=num)

            self.verify_stress_thread(cs_thread_pool=stress_queue)
            self.get_stress_results(queue=stress_queue)

            self.db_cluster.flush_all_nodes()
            #time.sleep(60) if smaller_dataset else time.sleep(600)

            current_usage, current_used = self.get_max_disk_usage()
            self.log.info(f"Current max disk usage after writing to {ks_name}: {current_usage}% ({current_used} GB / {target_used_size} GB)")

    def log_disk_usage(self):
        # Headers
        headers = f"{'Node':<8} {'Total GB':<12} {'Used GB':<12} {'Avail GB':<12} {'Used %':<8}"
        self.log.info(headers)

        # Track totals
        total_gb = 0
        total_used = 0
        total_avail = 0
        used_percents = []

        # Node rows
        for idx, node in enumerate(self.db_cluster.nodes, 1):
            info = self.get_disk_info(node)
            data = f"{idx:<8} {info['total']:<12} {info['used']:<12} {info['available']:<12} {info['used_percent']:.1f}%"
            self.log.info(data)
            
            total_gb += info['total']
            total_used += info['used']
            total_avail += info['available']
            used_percents.append(info['used_percent'])

        # Cluster row
        avg_used_percent = sum(used_percents) / len(used_percents)
        total = f"Cluster  {total_gb:<12} {total_used:<12} {total_avail:<12} {avg_used_percent:.1f}%"
        self.log.info(total)

    def test_data_removal_compaction(self):
        """
        3 nodes cluster, RF=3.
        Write data until 90% disk usage is reached.
        Sleep for 60 minutes.
        Drop some data and verify space was reclaimed
        """
        self.run_stress(self.softlimit, sleep_time=self.sleep_time_fill_disk)
        self.run_stress(self.hardlimit, sleep_time=self.sleep_time_fill_disk)

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
