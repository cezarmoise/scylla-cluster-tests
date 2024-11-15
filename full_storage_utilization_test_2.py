import time

from full_storage_utilization_test import FullStorageUtilizationTest
from sdcm.utils.tablets.common import wait_for_tablets_balanced


class FullStorageUtilizationTest2(FullStorageUtilizationTest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_removal_action = self.params.get('data_removal_action')
        self.scale_out_instance_type = self.params.get('scale_out_instance_type')
        self.scale_out_dc_idx = self.params.get("scale_out_dc_idx")

    def get_total_free_space(self):
        free = 0
        for node in self.db_cluster.nodes:
            info = self.get_disk_info(node)
            free += info["available"]

        return free

    def get_keyspaces(self, prefix):
        rows = self.execute_cql(f"SELECT keyspace_name FROM system_schema.keyspaces")
        return [row.keyspace_name for row in rows if row.keyspace_name.startswith(prefix)]
    
    def execute_cql(self, query):
        node = self.db_cluster.nodes[0]
        self.log.info(f"Executing {query}")
        with self.db_cluster.cql_connection_patient(node) as session:
            results = session.execute(query)

        return results
    
    def remove_data_from_cluster(self, keyspace):
        table = "standard1"
        match self.data_removal_action:
            case "drop":
                query = f"DROP TABLE {keyspace}.{table}"
            case "truncate":
                query = f"TRUNCATE TABLE {keyspace}.{table}"
            case "expire":
                query = f"ALTER TABLE {keyspace}.{table} WITH default_time_to_live = 300 AND gc_grace_seconds = 300"
            case _:
                raise ValueError(f"data_removal_action={self.data_removal_action} is not supported!")
        self.execute_cql(query)

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
        self.monitors.reconfigure_scylla_monitoring()
        wait_for_tablets_balanced(self.db_cluster.nodes[0])

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

        self.log_disk_usage() 

        self.scale_out()
        self.log_disk_usage() 

        time.sleep(1800)
        self.log_disk_usage() 
