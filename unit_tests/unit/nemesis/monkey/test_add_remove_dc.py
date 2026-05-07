"""Tests for sdcm.nemesis.monkey.add_remove_dc module."""

from unittest.mock import MagicMock, patch

import pytest

from sdcm.exceptions import KillNemesis, UnsupportedNemesis
from sdcm.nemesis.monkey.add_remove_dc import AddRemoveDcNemesis
from sdcm.utils.replication_strategy_utils import (
    NetworkTopologyReplicationStrategy,
    ReplicationStrategy,
    SimpleReplicationStrategy,
)

_MODULE = "sdcm.nemesis.monkey.add_remove_dc"

pytestmark = pytest.mark.usefixtures("events")


def _clone_strategy(strategy):
    if isinstance(strategy, NetworkTopologyReplicationStrategy):
        return NetworkTopologyReplicationStrategy(**strategy.replication_factors_per_dc)
    return SimpleReplicationStrategy(strategy.replication_factors[0])


class FakeReplicationStrategySetter:
    """Context manager that records keyspace strategy updates."""

    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.calls = []
        self.preserved = {}
        self.rollback_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.rollback_calls.append(self.preserved.copy())
        self.events.append(f"{self.name}:rollback")
        return False

    def __call__(self, **keyspaces):
        self.calls.append(keyspaces)
        for keyspace, strategy in keyspaces.items():
            if keyspace not in self.preserved:
                self.preserved[keyspace] = _clone_strategy(ReplicationStrategy.get(None, keyspace))


@pytest.fixture()
def runner(base_runner):
    """Base runner extended with add/remove-dc specific cluster state."""
    status_by_dc = {"dc1": object()}

    base_runner.cluster.test_config = MagicMock(MULTI_REGION=False)
    base_runner.cluster.nodes = list(base_runner.cluster.data_nodes)
    base_runner.decommission_nodes = MagicMock()
    base_runner.run_repair = MagicMock()
    base_runner.current_disruption = "AddRemoveDcNemesis"
    base_runner.monitoring_set = MagicMock()

    base_runner.target_node.raft = MagicMock(is_consistent_topology_changes_enabled=True)

    base_runner.tester.db_cluster = MagicMock()
    base_runner.tester.db_cluster.get_nodetool_status.side_effect = lambda: dict(status_by_dc)

    base_runner.status_by_dc = status_by_dc
    return base_runner


def test_disrupt_raises_unsupported_for_multi_region(runner):
    """MULTI_REGION runs are rejected before any topology changes start."""
    runner.cluster.test_config.MULTI_REGION = True

    with pytest.raises(UnsupportedNemesis, match="multi-dc scenario"):
        AddRemoveDcNemesis(runner).disrupt()

    runner.tester.create_keyspace.assert_not_called()
    runner.cluster.add_nodes.assert_not_called()
    runner.run_repair.assert_not_called()
    runner.decommission_nodes.assert_not_called()
    assert not runner.executed


def test_disrupt_updates_replication_and_cleans_new_dc(runner):
    """disrupt() should update replication for the temporary DC and remove it afterwards."""
    monkey = AddRemoveDcNemesis(runner)
    new_nodes = [MagicMock(name="node-new-1"), MagicMock(name="node-new-2")]
    events = []
    first_setter = FakeReplicationStrategySetter("system-keyspaces", events)
    second_setter = FakeReplicationStrategySetter("new-dc-rf", events)

    def add_nodes():
        monkey.new_nodes = new_nodes
        runner.status_by_dc["dc1_nemesis_dc"] = object()

    def decommission_nodes():
        events.append("decommission")
        runner.decommission_nodes(new_nodes)
        monkey.new_nodes = []
        runner.status_by_dc.pop("dc1_nemesis_dc", None)

    monkey.add_nodes_in_new_dc = MagicMock(side_effect=add_nodes)
    monkey.decommission_new_nodes = MagicMock(side_effect=decommission_nodes)
    monkey.rebuild_new_dc = MagicMock()
    monkey.write_to_multi_dc_keyspace = MagicMock()
    monkey.verify_multi_dc_keyspace = MagicMock()

    strategies = [
        SimpleReplicationStrategy(monkey.new_ks_rf),
        SimpleReplicationStrategy(monkey.new_ks_rf),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
        NetworkTopologyReplicationStrategy(**{monkey.initial_dc_name: monkey.new_ks_rf}),
    ]

    with (
        patch(f"{_MODULE}.temporary_replication_strategy_setter", side_effect=[first_setter, second_setter]),
        patch(f"{_MODULE}.ReplicationStrategy.get", side_effect=strategies),
    ):
        monkey.disrupt()

    runner.tester.create_keyspace.assert_called_once_with(
        monkey.new_ks_name,
        replication_factor={monkey.initial_dc_name: monkey.new_ks_rf},
    )
    monkey.add_nodes_in_new_dc.assert_called_once_with()
    assert monkey.rebuild_new_dc.call_args_list == [((new_nodes[0],),), ((new_nodes[1],),)]
    runner.run_repair.assert_called_once_with()
    monkey.write_to_multi_dc_keyspace.assert_called_once_with()
    monkey.decommission_new_nodes.assert_called_once_with()
    monkey.verify_multi_dc_keyspace.assert_called_once_with(consistency_level="QUORUM")

    assert list(first_setter.calls[0]) == ["system_distributed"]
    switched_strategy = first_setter.calls[0]["system_distributed"]
    assert isinstance(switched_strategy, NetworkTopologyReplicationStrategy)
    assert switched_strategy.replication_factors_per_dc == {monkey.initial_dc_name: monkey.new_ks_rf}
    preserved_strategy = first_setter.preserved["system_distributed"]
    assert isinstance(preserved_strategy, SimpleReplicationStrategy)
    assert preserved_strategy.replication_factors == [monkey.new_ks_rf]

    updated_keyspaces = [next(iter(call)) for call in second_setter.calls]
    assert updated_keyspaces == ["system_distributed", "system_traces", monkey.new_ks_name]
    for call in second_setter.calls:
        strategy = next(iter(call.values()))
        assert strategy.replication_factors_per_dc == {
            monkey.initial_dc_name: monkey.new_ks_rf,
            "dc1_nemesis_dc": monkey.num_nodes_in_new_dc,
        }

    for preserved_strategy in second_setter.preserved.values():
        assert preserved_strategy.replication_factors_per_dc == {
            monkey.initial_dc_name: monkey.new_ks_rf,
            "dc1_nemesis_dc": 0,
        }

    assert len(second_setter.rollback_calls) == 1
    assert list(second_setter.rollback_calls[0]) == updated_keyspaces
    assert events.index("new-dc-rf:rollback") < events.index("decommission")

    assert monkey.new_dc_name is None


def test_finalizer_drops_keyspace_and_decommissions_new_nodes(runner):
    """finalizer() should clean up the keyspace and temporary nodes on regular exits."""
    monkey = AddRemoveDcNemesis(runner)
    monkey.new_nodes = [MagicMock(name="node-new")]
    monkey.decommission_new_nodes = MagicMock()

    monkey.finalizer(Exception, None, None)

    assert runner.executed[-1] == f"DROP KEYSPACE IF EXISTS {monkey.new_ks_name}"
    monkey.decommission_new_nodes.assert_called_once_with()


def test_finalizer_skips_cleanup_on_kill_nemesis(runner):
    """finalizer() should not drop keyspace or decommission when KillNemesis is raised."""
    monkey = AddRemoveDcNemesis(runner)
    monkey.new_nodes = [MagicMock(name="node-new")]
    monkey.decommission_new_nodes = MagicMock()

    monkey.finalizer(KillNemesis, None, None)

    assert not runner.executed
    monkey.decommission_new_nodes.assert_not_called()


def test_system_keyspaces_includes_auth_when_topology_changes_disabled(runner):
    """system_keyspaces should include system_auth when consistent topology changes are off."""
    runner.target_node.raft.is_consistent_topology_changes_enabled = False
    monkey = AddRemoveDcNemesis(runner)

    assert "system_auth" in monkey.system_keyspaces


def test_system_keyspaces_excludes_auth_when_topology_changes_enabled(runner):
    """system_keyspaces should NOT include system_auth when consistent topology changes are on."""
    runner.target_node.raft.is_consistent_topology_changes_enabled = True
    monkey = AddRemoveDcNemesis(runner)

    assert "system_auth" not in monkey.system_keyspaces
