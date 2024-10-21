import time
from sdcm.tester import ClusterTester
from sdcm.utils.tablets.common import wait_for_tablets_balanced


class FullStorageUtilizationTest(ClusterTester):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sleep_time_before_autoscale = 120
        self.sleep_time_fill_disk = 1800
        self.soft_limit = self.params.get('diskusage_softlimit') if self.params.get('diskusage_softlimit') is not None else 70
        self.hard_limit = self.params.get('diskusage_hardlimit') if self.params.get('diskusage_hardlimit') is not None else 90
        self.stress_cmd_throttled = self.params.get('stress_cmd')
        self.add_node_cnt = self.params.get('add_node_cnt')
        self.auto_scaling_action_type = self.params.get('auto_scaling_action_type')

    def prepare_dataset_layout(self, dataset_size, row_size=10240):
        n = dataset_size * 1024 * 1024 * 1024 // row_size
        seq_end = n * 100

        return f'cassandra-stress write cl=ONE n={n} -mode cql3 native -rate threads=10 -pop dist="uniform(1..{seq_end})" ' \
               f'-col "size=FIXED({row_size}) n=FIXED(1)" -schema "replication(strategy=NetworkTopologyStrategy,replication_factor=3)"'

    def setUp(self):
        super().setUp()
        self.start_time = time.time()

    def start_throttle_write(self):
        self.run_stress_thread(stress_cmd=self.stress_cmd_throttled)
        self.log.info("Wait for 2 mins for the stress command to start")
        time.sleep(self.sleep_time_before_autoscale)        
        
    def scale_out(self):
        self.start_throttle_write()
        self.log.info("Adding a new node")
        self.add_new_node()

    def scale_in(self):        
        #self.start_throttle_write()
        self.log.info("Removing a node")
        self.remove_node()

    def drop_data(self, keyspace_name):
        '''
        Drop keyspace and clear snapshots.
        '''
        node = self.db_cluster.nodes[0]
        self.log.info("Dropping some data")
        cql = f"DROP KEYSPACE {keyspace_name}"
        node.run_cqlsh(cql)
        #node.run_nodetool(f"clearsnapshot")

    def perform_action(self):        
        self.log_disk_usage()
        # Trigger specific action
        if self.auto_scaling_action_type == "scale_out":
           self.scale_out()
        elif self.auto_scaling_action_type == "scale_in":            
           self.scale_out()           
           '''
           Before removing a node, we should make sure
           other nodes has enough space so that they
           can accommodate data from the removed node.
           '''
           # Remove 20% of data from the cluster.
           self.drop_data("keyspace_large1")
           self.drop_data("keyspace_large2")
           self.scale_in()   
        self.log_disk_usage()     

    def test_storage_utilization(self):
        """
        3 nodes cluster, RF=3.
        Write data until 90% disk usage is reached.
        Sleep for 60 minutes.
        Perform specific action.
        """
        self.run_stress(self.soft_limit, sleep_time=self.sleep_time_fill_disk)
        self.run_stress(self.hard_limit, sleep_time=self.sleep_time_fill_disk)
        self.perform_action()        

    def run_stress(self, target_usage, sleep_time=600):
        target_used_size = self.calculate_target_used_size(target_usage)
        self.run_stress_until_target(target_used_size, target_usage)

        self.log_disk_usage()
        self.log.info(f"Wait for {sleep_time} seconds")
        time.sleep(sleep_time)  
        self.log_disk_usage()

    def run_stress_until_target(self, target_used_size, target_usage):
        current_usage, current_used = self.get_max_disk_usage()
        num = 0
        smaller_dataset = False
        
        space_needed = target_used_size - current_used
        # Calculate chunk size as 10% of space needed
        chunk_size = int(space_needed * 0.1)
        while current_used < target_used_size and current_usage < target_usage:
            num += 1
            
            # Write smaller dataset near the threshold (15% or 30GB of the target)
            smaller_dataset = (((target_used_size - current_used) < 30) or ((target_usage - current_usage) <= 15))

            # Use 1GB chunks near threshold, otherwise use 10% of remaining space
            dataset_size = 1 if smaller_dataset else chunk_size
            ks_name = "keyspace_small" if smaller_dataset else "keyspace_large"
            self.log.info(f"Writing chunk of size: {dataset_size} GB")
            stress_cmd = self.prepare_dataset_layout(dataset_size)
            stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, keyspace_name=f"{ks_name}{num}", stress_num=1, keyspace_num=num)

            self.verify_stress_thread(cs_thread_pool=stress_queue)
            self.get_stress_results(queue=stress_queue)

            self.flush_all_nodes()
            #time.sleep(60) if smaller_dataset else time.sleep(600)

            current_usage, current_used = self.get_max_disk_usage()
            self.log.info(f"Current max disk usage after writing to keyspace{num}: {current_usage}% ({current_used} GB / {target_used_size} GB)")

    def add_new_node(self):
        new_nodes = self.db_cluster.add_nodes(count=self.add_node_cnt, enable_auto_bootstrap=True)
        self.db_cluster.wait_for_init(node_list=new_nodes)
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=new_nodes)
        total_nodes_in_cluster = len(self.db_cluster.nodes)
        self.log.info(f"New node added, total nodes in cluster: {total_nodes_in_cluster}")
        self.monitors.reconfigure_scylla_monitoring()
        wait_for_tablets_balanced(self.db_cluster.nodes[0])
        
    def remove_node(self):  
        self.log.info('Removing a second node from the cluster')
        node_to_remove = self.db_cluster.nodes[1]
        self.log.info(f"Node to be removed: {node_to_remove.name}")
        self.db_cluster.decommission(node_to_remove)
        self.log.info(f"Node {node_to_remove.name} has been removed from the cluster")
        self.monitors.reconfigure_scylla_monitoring()
        wait_for_tablets_balanced(self.db_cluster.nodes[0])
    
    def flush_all_nodes(self):
        for node in self.db_cluster.nodes:
            self.log.info(f"Flushing data on node {node.name}")
            node.run_nodetool("flush")

    def get_max_disk_usage(self):
        max_usage = 0
        max_used = 0
        for node in self.db_cluster.nodes:
            result = node.remoter.run("df -h --output=used,pcent /var/lib/scylla | sed 1d | sed 's/G//' | sed 's/%//'")
            used, usage = result.stdout.strip().split()
            max_usage = max(max_usage, int(usage))
            max_used = max(max_used, int(used))
        return max_usage, max_used

    def get_disk_info(self, node):
        result = node.remoter.run("df -h --output=size,used,avail,pcent /var/lib/scylla | sed 1d | sed 's/G//g' | sed 's/%//'")
        size, used, avail, pcent = result.stdout.strip().split()
        return {
            'total': int(size),
            'used': int(used),
            'available': int(avail),
            'used_percent': int(pcent)
        }

    def calculate_target_used_size(self, target_percent):
        max_total = 0
        for node in self.db_cluster.nodes:
            info = self.get_disk_info(node)
            max_total = max(max_total, info['total'])
        
        target_used_size = (target_percent / 100) * max_total
        current_usage, current_used = self.get_max_disk_usage()
        additional_usage_needed = target_used_size - current_used

        self.log.info(f"Current max disk usage: {current_usage:.2f}%")
        self.log.info(f"Current max used space: {current_used:.2f} GB")
        self.log.info(f"Max total disk space: {max_total:.2f} GB")
        self.log.info(f"Target used space to reach {target_percent}%: {target_used_size:.2f} GB")
        self.log.info(f"Additional space to be used: {additional_usage_needed:.2f} GB")

        return target_used_size

    def log_disk_usage(self):
        for node in self.db_cluster.nodes:
            info = self.get_disk_info(node)
            self.log.info(f"Disk usage for node {node.name}:")
            self.log.info(f"  Total: {info['total']} GB")
            self.log.info(f"  Used: {info['used']} GB")
            self.log.info(f"  Available: {info['available']} GB")
            self.log.info(f"  Used %: {info['used_percent']}%")

    
