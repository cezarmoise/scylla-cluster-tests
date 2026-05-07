from contextlib import ExitStack

from sdcm.cluster import BaseNode
from sdcm.exceptions import KillNemesis, UnsupportedNemesis
from sdcm.nemesis import NemesisBaseClass
from sdcm.provision.scylla_yaml import SeedProvider
from sdcm.sct_events.system import InfoEvent
from sdcm.utils.decorators import skip_on_capacity_issues
from sdcm.utils.replication_strategy_utils import (
    NetworkTopologyReplicationStrategy,
    ReplicationStrategy,
    temporary_replication_strategy_setter,
)
from sdcm.wait import wait_for_log_lines


class AddRemoveDcNemesis(NemesisBaseClass):
    disruptive = True
    limited = True
    topology_changes = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.new_nodes: list[BaseNode] = []
        self.num_nodes_in_new_dc: int = 2
        self.initial_dc_name: str = self.datacenters[0]
        self.new_ks_name: str = "keyspace_new_dc"
        self.new_ks_rf: int = len(self.runner.cluster.racks)

    @property
    def system_keyspaces(self) -> list[str]:
        system_keyspaces = ["system_distributed", "system_traces"]
        if (
            not self.runner.target_node.raft.is_consistent_topology_changes_enabled
        ):  # auth-v2 is used when consistent topology is enabled
            system_keyspaces.append("system_auth")
        return system_keyspaces

    @property
    def datacenters(self) -> list[str]:
        return list(self.runner.tester.db_cluster.get_nodetool_status().keys())

    @property
    def new_dc_name(self) -> str | None:
        if has_suffix := [dc for dc in self.datacenters if dc.endswith("_nemesis_dc")]:
            return has_suffix[0]
        return None

    def configure_new_dc(self, node: BaseNode):
        """Configure node for new datacenter before Scylla starts."""
        with node.remote_scylla_yaml() as scylla_yml:
            scylla_yml.rpc_address = node.ip_address
            scylla_yml.seed_provider = [
                SeedProvider(
                    class_name="org.apache.cassandra.locator.SimpleSeedProvider",
                    parameters=[{"seeds": ",".join(self.runner.tester.db_cluster.seed_nodes_addresses)}],
                )
            ]

        endpoint_snitch = self.runner.cluster.params.get("endpoint_snitch") or ""
        if endpoint_snitch.endswith("GossipingPropertyFileSnitch"):
            rackdc_value = {"dc": "add_remove_nemesis_dc"}
        else:
            rackdc_value = {"dc_suffix": "_nemesis_dc"}

        with node.remote_cassandra_rackdc_properties() as properties_file:
            properties_file.update(**rackdc_value)

    def add_nodes_in_new_dc(self):
        add_node_func_args = {
            "count": self.num_nodes_in_new_dc,
            "dc_idx": 0,
            "rack": None,
            "enable_auto_bootstrap": True,
            "disruption_name": self.runner.current_disruption,
            "after_config": self.configure_new_dc,
        }
        self.new_nodes = skip_on_capacity_issues(db_cluster=self.runner.tester.db_cluster)(
            self.runner.cluster.add_nodes
        )(**add_node_func_args)
        # wait_for_init() will call node_setup(), which executes the callback after config_setup()
        self.runner.cluster.wait_for_init(node_list=self.new_nodes, timeout=900, check_node_health=False)
        for new_node in self.new_nodes:
            new_node.wait_node_fully_start()
        self.runner.monitoring_set.reconfigure_scylla_monitoring()

    def rebuild_new_dc(self, new_node: BaseNode):
        cmd = f"rebuild -- {self.initial_dc_name}"
        with (
            wait_for_log_lines(
                node=new_node,
                start_line_patterns=["rebuild.*started with keyspaces=", "Rebuild starts"],
                end_line_patterns=["rebuild.*finished with keyspaces=", "Rebuild succeeded"],
                start_timeout=60,
                end_timeout=600,
            ),
            self.runner.action_log_scope(f"Run rebuild on {new_node.name} with cmd: {cmd}"),
        ):
            new_node.run_nodetool(sub_cmd=cmd, long_running=True, retry=0)

    def create_new_dc_keyspace(self):
        write_cmd = (
            f"cassandra-stress write no-warmup cl=ALL n=100000 -schema 'keyspace={self.new_ks_name} "
            f"replication(strategy=NetworkTopologyStrategy,{self.initial_dc_name}={self.new_ks_rf}) "
            f"compression=LZ4Compressor compaction(strategy=SizeTieredCompactionStrategy)' "
            f"-mode cql3 native compression=lz4 -rate threads=5 -pop seq=1..100000 -log interval=5"
        )
        write_thread = self.runner.tester.run_stress_thread(
            stress_cmd=write_cmd, round_robin=True, stop_test_on_failure=False
        )
        self.runner.tester.verify_stress_thread(write_thread, error_handler=self.runner._nemesis_stress_failure_handler)
        # flush data to ensure it is seen in monitoring
        for node in self.runner.cluster.nodes:
            with self.runner.action_log_scope(f"Flush data in {self.new_ks_name} on {node.name} node"):
                node.run_nodetool(f"flush {self.new_ks_name}")

    def write_to_multi_dc_keyspace(self) -> None:
        InfoEvent(message="Writing and reading data with new dc").publish()
        write_cmd = (
            f"cassandra-stress write no-warmup cl=ALL n=100000 -schema 'keyspace={self.new_ks_name} "
            f"replication(strategy=NetworkTopologyStrategy,{self.initial_dc_name}={self.new_ks_rf},{self.new_dc_name}={self.num_nodes_in_new_dc}) "
            f"compression=LZ4Compressor compaction(strategy=SizeTieredCompactionStrategy)' "
            f"-mode cql3 native compression=lz4 -rate threads=5 -pop seq=100001..200000 -log interval=5"
        )
        write_thread = self.runner.tester.run_stress_thread(
            stress_cmd=write_cmd, round_robin=True, stop_test_on_failure=False
        )
        self.runner.tester.verify_stress_thread(write_thread, error_handler=self.runner._nemesis_stress_failure_handler)
        with self.runner.action_log_scope("Verify multi DC keyspace data"):
            self.verify_multi_dc_keyspace(consistency_level="ALL")
        # flush data to ensure it is seen in monitoring
        for node in self.runner.cluster.nodes:
            with self.runner.action_log_scope(f"Flush data in {self.new_ks_name} on {node.name} node"):
                node.run_nodetool(f"flush {self.new_ks_name}")

    def verify_multi_dc_keyspace(self, consistency_level: str = "ALL"):
        read_cmd = (
            f"cassandra-stress read no-warmup cl={consistency_level} n=100000 -schema 'keyspace={self.new_ks_name} "
            f"compression=LZ4Compressor' -mode cql3 native compression=lz4 -rate threads=5 "
            f"-pop seq=100001..200000 -log interval=5"
        )
        read_thread = self.runner.tester.run_stress_thread(
            stress_cmd=read_cmd, round_robin=True, stop_test_on_failure=False
        )
        self.runner.tester.verify_stress_thread(read_thread, error_handler=self.runner._nemesis_stress_failure_handler)

    def decommission_new_nodes(self):
        self.runner.decommission_nodes(self.new_nodes)
        self.new_nodes = []

    def finalizer(self, exc_type, *_):
        # in case of test end/killed, leave the cleanup alone
        if exc_type is not KillNemesis:
            with self.runner.cluster.cql_connection_patient(self.runner.target_node) as session:
                session.execute(f"DROP KEYSPACE IF EXISTS {self.new_ks_name}")
            if self.new_nodes:
                self.decommission_new_nodes()

    def disrupt(self) -> None:
        if self.runner.cluster.test_config.MULTI_REGION:
            raise UnsupportedNemesis(
                "add_remove_dc skipped for multi-dc scenario (https://github.com/scylladb/scylla-cluster-tests/issues/5369)"
            )
        InfoEvent(message="Starting New DC Nemesis").publish()
        with ExitStack() as context_manager:
            context_manager.push(self.finalizer)

            with self.runner.action_log_scope("Add new keyspace"):
                self.runner.tester.create_keyspace(
                    self.new_ks_name, replication_factor={self.initial_dc_name: self.new_ks_rf}
                )

            with self.runner.action_log_scope("Add nodes in new DC"):
                self.add_nodes_in_new_dc()
            assert self.new_dc_name, "new datacenter was not registered"

            # switch system keyspaces to NetworkTopologyStrategy
            with (
                temporary_replication_strategy_setter(self.runner.target_node) as ntrs_setter,
                self.runner.action_log_scope("Temporarily switch system keyspaces to NetworkTopologyStrategy"),
            ):
                for keyspace in self.system_keyspaces:
                    old_rs = ReplicationStrategy.get(self.runner.target_node, keyspace)
                    if not isinstance(old_rs, NetworkTopologyReplicationStrategy):
                        InfoEvent(message=f"Switching {keyspace} to NetworkTopologyReplicationStrategy").publish()
                        new_rs = NetworkTopologyReplicationStrategy(
                            **{self.initial_dc_name: old_rs.replication_factors[0]}
                        )
                        ntrs_setter(**{keyspace: new_rs})

                # add the new DC to the replication strategy
                with (
                    temporary_replication_strategy_setter(self.runner.target_node) as replication_strategy_setter,
                    self.runner.action_log_scope("Temporarily update replication factors for new datacenter"),
                ):
                    for keyspace in self.system_keyspaces + [self.new_ks_name]:
                        strategy = ReplicationStrategy.get(self.runner.target_node, keyspace)
                        InfoEvent(message="Updating replication factors for new datacenter").publish()
                        strategy.replication_factors_per_dc.update({self.new_dc_name: self.num_nodes_in_new_dc})
                        with self.runner.action_log_scope(f"Set replication strategy for '{keyspace}' to {strategy}"):
                            replication_strategy_setter(**{keyspace: strategy})

                    InfoEvent(
                        message=f"Replication strategy for {self.new_ks_name} is {ReplicationStrategy.get(self.runner.target_node, self.new_ks_name)}"
                    ).publish()

                    # when context exits, set RF=0 for the new DC
                    for _, preserved_strategy in replication_strategy_setter.preserved.items():
                        preserved_strategy.replication_factors_per_dc[self.new_dc_name] = 0

                    InfoEvent(message="Running rebuild on new datacenter").publish()
                    for node in self.new_nodes:
                        self.rebuild_new_dc(node)

                    InfoEvent(message="Running full cluster repair").publish()
                    self.runner.run_repair()

                    with self.runner.action_log_scope("Run write and then read data to multiDC keyspace"):
                        self.write_to_multi_dc_keyspace()

                with self.runner.action_log_scope(f"Decommission nodes in the new datacenter {self.new_dc_name}"):
                    self.decommission_new_nodes()
                assert not self.new_dc_name, "new datacenter was not unregistered"

                with self.runner.action_log_scope("Verify keyspace data after decommissioning"):
                    self.verify_multi_dc_keyspace(consistency_level="QUORUM")
