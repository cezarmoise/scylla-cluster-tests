import time

from full_storage_utilization_test import FullStorageUtilizationTest


class FullStorageUtilizationTest2(FullStorageUtilizationTest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_removal_action = self.params.get('data_removal_action')

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

    def reclaim_space(self):
        for node in self.db_cluster.nodes:
            node.run_nodetool("cleanup")
            node.run_nodetool("compact")

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

        self.reclaim_space()

        # sleep to let compaction happen
        time.sleep(1800)
        
        self.log_disk_usage() 

        free_after = self.get_total_free_space()
        assert free_after > free_before, "space was not freed after dropping data"
