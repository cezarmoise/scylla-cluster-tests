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
# Copyright (c) 2026 ScyllaDB

import json
import textwrap
import time
from functools import cached_property
from typing import Literal

import yaml

from sdcm import wait
from sdcm.argus_results import (
    ManagerRestoreBenchmarkResult,
    send_manager_benchmark_results_to_argus,
    send_manager_snapshot_details_to_argus,
)
from sdcm.cluster import HOUR_IN_SEC
from sdcm.mgmt.operations import ManagerTestFunctionsMixIn, SnapshotData
from sdcm.rest.remote_curl_client import RemoteCurlClient
from sdcm.utils.cluster_tools import flush_nodes
from sdcm.utils.replication_strategy_utils import ReplicationStrategy
from sdcm.utils.time_utils import ExecutionTimer

TASK_TIMEOUT = 200000


class SingleCopyTest(ManagerTestFunctionsMixIn):
    """Single copy (tablet-aware, deduplicated) backup and restore, driven via Scylla REST API only.

    Backup tests write `mgmt_prepare_snapshot_size` GB, part of it repaired by incremental repair, so the backup
    holds repaired sstables (uploaded from one replica only) and unrepaired ones (uploaded from every replica).
    They log the snapshot entry for `defaults/manager_restore_benchmark_snapshots.yaml`.
    Restore tests restore the snapshot set in `mgmt_reuse_backup_snapshot_name`.
    Requires a Scylla build with cluster backup and tablet-aware restore of its layout (scylladb#31138).
    """

    @cached_property
    def api_node(self):
        return self.db_cluster.data_nodes[0]

    @cached_property
    def location(self) -> str:
        return self.locations[0]

    @cached_property
    def bucket(self) -> str:
        return self.location.split(":")[-1]

    @property
    def keyspaces(self) -> list[str]:
        return self.db_cluster.get_test_keyspaces()

    @cached_property
    def snapshot(self) -> SnapshotData:
        snapshot_name = self.params.get("mgmt_reuse_backup_snapshot_name")
        assert snapshot_name, "Restore tests require `mgmt_reuse_backup_snapshot_name`"
        snapshot = self.get_snapshot_data(snapshot_name)
        assert snapshot.prefix, f"Snapshot {snapshot_name} has no `prefix`, it is not a Scylla API backup"
        return snapshot

    def _scylla_api(self, method: Literal["GET", "POST"], path: str, params: dict | None = None, data=None, node=None):
        """Call Scylla REST API, `path` starts with the API endpoint, e.g. `storage_service/tablets/backup`."""
        endpoint, _, path = path.partition("/")
        client = RemoteCurlClient(host="localhost:10000", endpoint=endpoint, node=node or self.api_node)
        stdout = client.run_remoter_curl(method=method, path=path, params=params, data=data, retry=3).stdout.strip()
        if not stdout:
            return None
        # curl retries on HTTP 500 and concatenates the error responses, take the first one
        response, _ = json.JSONDecoder().raw_decode(stdout)
        # errors come back as {"message": ..., "code": ...} while curl exits with 0
        if isinstance(response, dict) and {"message", "code"} <= response.keys():
            raise AssertionError(f"Scylla API {method} {endpoint}/{path} {params} failed: {response}")
        return response

    def _get_task_status(self, task_id: str, node=None) -> dict:
        status = self._scylla_api("GET", f"task_manager/task_status/{task_id}", node=node)
        self.log.debug("Task %s status: %s", task_id, status)
        return status

    def _wait_for_task(self, task_id: str, node=None, timeout: int = TASK_TIMEOUT, step: int = 30) -> dict:
        """Wait for the task to finish, return its final status."""

        def _finished_status():
            status = self._get_task_status(task_id, node=node)
            return status if status["state"] in ("done", "failed") else None

        return wait.wait_for(_finished_status, step=step, timeout=timeout, text=f"Waiting for Scylla task {task_id}")

    def _assert_task_done(self, task_id: str, node=None, timeout: int = TASK_TIMEOUT) -> dict:
        status = self._wait_for_task(task_id, node=node, timeout=timeout)
        assert status["state"] == "done", f"Task {task_id} failed: {status.get('error')}"
        return status

    def _abort_task_in_progress(self, task_id: str) -> None:
        """Wait until the task makes progress, abort it and check it fails with an abort error."""

        def _in_progress_or_finished_status():
            status = self._get_task_status(task_id)
            finished = status["state"] in ("done", "failed")
            return status if finished or status["progress_completed"] > 0 else None

        status = wait.wait_for(
            _in_progress_or_finished_status,
            step=5,
            timeout=HOUR_IN_SEC,
            text=f"Waiting for task {task_id} to make progress",
        )
        assert status["state"] == "running", f"Task {task_id} finished before abort: {status}"
        assert status["is_abortable"], f"Task {task_id} is not abortable: {status}"

        self.log.info(
            "Abort task %s at progress %s/%s", task_id, status["progress_completed"], status["progress_total"]
        )
        self._scylla_api("POST", f"task_manager/abort_task/{task_id}")

        status = self._wait_for_task(task_id, timeout=HOUR_IN_SEC, step=5)
        assert status["state"] == "failed", f"Task {task_id} is expected to fail after abort: {status}"
        assert "abort" in status["error"].lower(), f"Task {task_id} failed with unexpected error: {status['error']}"
        assert status["progress_completed"] < status["progress_total"], (
            f"Task {task_id} completed despite abort: {status}"
        )

    def _get_object_storage_endpoint(self) -> str:
        """Name of the object storage endpoint configured in scylla.yaml."""
        endpoints = (self.params.get("append_scylla_yaml") or {}).get("object_storage_endpoints")
        assert endpoints, "Backup to object storage requires `object_storage_endpoints` in `append_scylla_yaml`"
        # `{region}` is filled with the first test region when writing scylla.yaml, see `sdcm/cluster.py`
        return endpoints[0]["name"].format(region=next(iter(self.params.region_names), ""))

    def _get_tables(self, keyspace: str) -> list[str]:
        with self.db_cluster.cql_connection_patient(self.api_node) as session:
            rows = session.execute("SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s", (keyspace,))
            return [row.table_name for row in rows]

    def _get_rf(self, keyspace: str) -> int:
        return sum(ReplicationStrategy.get(self.api_node, keyspace).replication_factors)

    def _new_prefix(self, name: str) -> str:
        return f"single_copy/{self.test_config.test_id()}/{name}"

    def _get_prefix_objects(self, prefix: str) -> dict[str, int] | None:
        """Size of each object under the prefix, by its path relative to the prefix. None if the provider is not supported."""
        provider = self.location.split(":")[-2]
        if (lister := self._bucket_object_listers.get(provider)) is None:
            self.log.info("Listing bucket objects is not implemented for '%s' backend, skipping", provider)
            return None
        return {key.removeprefix(f"{prefix}/"): size for key, size in lister(self.bucket, [f"{prefix}/"])}

    def _get_prefix_size_on_bucket(self, prefix: str) -> float | None:
        """Total size of objects under the prefix, in GiB. None if the provider is not supported."""
        if (objects := self._get_prefix_objects(prefix)) is None:
            return None
        size_gib = round(sum(objects.values()) / 1024**3, 3)
        self.log.info("Size on bucket under %s: %s GiB", prefix, size_gib)
        return size_gib

    def _assert_aborted_backup_is_partial(self, aborted_prefix: str, full_prefix: str) -> None:
        """Compare the objects of an aborted backup with a full backup of the same data, taken after it.

        The aborted backup must have uploaded no snapshot metadata (manifests) and only part of the sstables,
        each of them complete, i.e. present in the full backup with the same size.
        """
        if (aborted := self._get_prefix_objects(aborted_prefix)) is None:
            return
        full_sstables = {
            key: size for key, size in self._get_prefix_objects(full_prefix).items() if key.startswith("sstables/")
        }
        self.log.info(
            "Aborted backup uploaded %s objects of %s GiB, the full backup has %s sstable objects of %s GiB",
            len(aborted),
            round(sum(aborted.values()) / 1024**3, 3),
            len(full_sstables),
            round(sum(full_sstables.values()) / 1024**3, 3),
        )
        metadata = sorted(key for key in aborted if not key.startswith("sstables/"))
        assert not metadata, f"Aborted backup uploaded snapshot metadata: {metadata}"
        unexpected = sorted(aborted.items() - full_sstables.items())
        assert not unexpected, (
            f"Aborted backup uploaded {len(unexpected)} objects missing from the full backup or of different size, "
            f"e.g. {unexpected[:5]}"
        )
        assert len(aborted) < len(full_sstables), (
            f"Aborted backup uploaded all {len(full_sstables)} sstable objects of the full backup"
        )

    def _repair(self) -> None:
        for keyspace in self.keyspaces:
            self.api_node.run_nodetool(
                sub_cmd="cluster repair",
                args=f"--incremental-mode incremental {keyspace}",
                timeout=HOUR_IN_SEC * 6,
                long_running=True,
                retry=0,
            )

    def _assert_tablets_repaired(self) -> None:
        """Single copy dedup relies on incremental repair setting the per-tablet repair watermark."""
        with self.db_cluster.cql_connection_patient(self.api_node) as session:
            for keyspace in self.keyspaces:
                rows = list(
                    session.execute(
                        f"SELECT table_name, sstables_repaired_at FROM system.tablets "
                        f"WHERE keyspace_name = '{keyspace}' ALLOW FILTERING"
                    )
                )
                assert rows, f"Keyspace {keyspace} has no tablets, single copy backup requires tablets"
                unrepaired = {row.table_name for row in rows if not row.sstables_repaired_at}
                assert not unrepaired, f"Tablets of {keyspace} tables {unrepaired} have no repair watermark"

    def _repair_written_data(self) -> None:
        self.log.info("Run incremental repair")
        self._repair()
        self._assert_tablets_repaired()

    def _flush_and_wait_for_compactions(self) -> None:
        if not self.params.get("mgmt_flush_and_wait_for_compactions"):
            return
        # flush first, otherwise the backup snapshot flushes memtables and triggers new compactions
        for keyspace in self.keyspaces:
            flush_nodes(cluster=self.db_cluster, keyspace=keyspace)
        self.wait_no_compactions_running(n=240)

    def _write_data(self, repaired_percent: int) -> None:
        """Write `mgmt_prepare_snapshot_size` GB, the first `repaired_percent` of it is repaired, the rest is not.

        Without `mgmt_prepare_snapshot_size` (e.g. user profiles of multiple tables), write `prepare_write_cmd`,
        repair it, then write `stress_cmd`.
        Then, if `mgmt_flush_and_wait_for_compactions` is set, flush and wait for compactions to settle,
        so they don't slow down the backup.
        """
        if not (size := self.params.get("mgmt_prepare_snapshot_size")):
            assert repaired_percent == 50, "Without `mgmt_prepare_snapshot_size` only 50% repaired data is supported"
            self.run_prepare_write_cmd()
            self._repair_written_data()
            self.run_and_verify_stress_in_threads(cs_cmds=self.params.get("stress_cmd"), stop_on_failure=True)
            self._flush_and_wait_for_compactions()
            return

        num_of_rows = size * 1024 * 1024
        repaired_rows = num_of_rows * repaired_percent // 100
        repaired_rows -= repaired_rows % self._get_total_loaders()  # rows per loader must be whole
        if repaired_rows:
            self.log.info("Write %s%% of %s GB, to be repaired", repaired_percent, size)
            _, cs_cmds = self.build_snapshot_preparer_cs_write_cmd(size, rows_range=(1, repaired_rows))
            self.run_and_verify_stress_in_threads(cs_cmds=cs_cmds, stop_on_failure=True)
            self._repair_written_data()
        if repaired_rows < num_of_rows:
            self.log.info("Write %s%% of %s GB, left unrepaired", 100 - repaired_percent, size)
            _, cs_cmds = self.build_snapshot_preparer_cs_write_cmd(size, rows_range=(repaired_rows + 1, num_of_rows))
            self.run_and_verify_stress_in_threads(cs_cmds=cs_cmds, stop_on_failure=True)
        self._flush_and_wait_for_compactions()

    def _start_cluster_backup(self, snapshot_tag: str, prefix: str) -> str:
        """Start cluster backup, the snapshot is created by Scylla. Return the task id."""
        endpoint = self._get_object_storage_endpoint()
        backup_location = [
            {"datacenter": dc, "endpoint": endpoint, "bucket": self.bucket, "prefix": prefix}
            for dc in sorted(self.get_all_dcs_names())
        ]
        # backup locations are read from the request body, despite the API doc listing them as a query param
        task_id = self._scylla_api(
            "POST",
            "storage_service/tablets/backup",
            params={"keyspace": ",".join(self.keyspaces), "snapshot": snapshot_tag},
            data=backup_location,
        )
        self.log.info("Cluster backup task %s started to %s/%s", task_id, self.bucket, prefix)
        return task_id

    def _cluster_backup(self, name: str = "backup") -> tuple[str, str, int]:
        """Run cluster backup to a new prefix. Return snapshot tag, prefix and duration in seconds."""
        snapshot_tag = f"sct_single_copy_{name}_{int(time.time())}"
        prefix = self._new_prefix(name)
        with ExecutionTimer() as timer:
            self._assert_task_done(self._start_cluster_backup(snapshot_tag, prefix))
        duration = int(timer.duration.total_seconds())
        self.log.info("Cluster backup took %s seconds", duration)
        return snapshot_tag, prefix, duration

    def _clear_snapshot(self, snapshot_tag: str) -> None:
        for node in self.db_cluster.data_nodes:
            node.run_nodetool("clearsnapshot", args=f"-t {snapshot_tag}")

    def _snapshot_cs_cmd(self, cl: str, num_of_rows: int) -> str:
        """C-S read command of the first `num_of_rows` rows of the snapshot dataset."""
        return self.snapshot.cs_read_cmd_template.format(
            **{
                **self.snapshot.dataset,
                "keyspace_name": self.snapshot.keyspaces[0],
                "cl": cl,
                "num_of_rows": num_of_rows,
                "sequence_start": 1,
                "sequence_end": num_of_rows,
            }
        )

    def _create_snapshot_schema(self) -> None:
        """Restore requires existing tables and the backup holds no schema: let c-s create it, then truncate."""
        assert "num_of_rows" in self.snapshot.dataset, "Schema creation supports only c-s `standard1` snapshots"
        stress_cmd = self._snapshot_cs_cmd(cl="QUORUM", num_of_rows=1).replace(
            "cassandra-stress read", "cassandra-stress write", 1
        )
        self.run_and_verify_stress_in_threads(cs_cmds=[stress_cmd], stop_on_failure=True)
        self._truncate_tables()

    def _truncate_tables(self) -> None:
        for keyspace, tables in self.snapshot.ks_tables_map.items():
            for table in tables:
                self.api_node.run_cqlsh(f"TRUNCATE {keyspace}.{table}", timeout=HOUR_IN_SEC)

    def _snapshot_manifests(self, keyspace: str, table: str) -> list[str]:
        prefix, tag = self.snapshot.prefix, self.snapshot.tag
        if node_ids := self.snapshot.node_ids:
            # per node backup, see `test_backup_old_format`
            return [f"{prefix}/{host_id}/{keyspace}/{table}/manifest.json" for host_id in node_ids]
        return [f"{prefix}/snapshots/{keyspace}/{table}/{tag}/manifest.json"]

    def _start_restore(self, keyspace: str, table: str) -> str:
        endpoint = self._get_object_storage_endpoint()
        bucket = self.snapshot.locations[0].split(":")[-1]
        # manifest keys are full paths in the bucket, hence the empty prefix
        restore_location = [
            {
                "datacenter": dc,
                "endpoint": endpoint,
                "bucket": bucket,
                "prefix": "",
                "manifests": self._snapshot_manifests(keyspace, table),
            }
            for dc in sorted(self.get_all_dcs_names())
        ]
        task_id = self._scylla_api(
            "POST",
            "storage_service/tablets/restore",
            params={"keyspace": keyspace, "table": table, "snapshot": self.snapshot.tag},
            data=restore_location,
        )
        self.log.info("Restore task %s of %s.%s started", task_id, keyspace, table)
        return task_id

    def _start_restore_all_tables(self) -> list[str]:
        return [
            self._start_restore(keyspace, table)
            for keyspace, tables in self.snapshot.ks_tables_map.items()
            for table in tables
        ]

    def _restore(self) -> int:
        """Restore every snapshot table in parallel and wait for all of them. Return duration in seconds."""
        with ExecutionTimer() as timer:
            for task_id in self._start_restore_all_tables():
                status = self._assert_task_done(task_id, timeout=self.snapshot.exp_timeout)
                assert 0 < status["progress_completed"] == status["progress_total"], (
                    f"Restore task {task_id} did not restore everything: {status}"
                )
        duration = int(timer.duration.total_seconds())
        self.log.info("Restore took %s seconds", duration)
        return duration

    def _verify_data_on_each_node(self) -> None:
        """Read the whole snapshot dataset with CL=ONE from each node only, in parallel."""
        if self.snapshot.prohibit_verification_read:
            self.log.info("Skip verification read, prohibited by the snapshot")
            return
        stress_read_cmd = self._snapshot_cs_cmd(cl="ONE", num_of_rows=self.snapshot.dataset["num_of_rows"])
        stress_queue = [
            self.run_stress_thread(
                stress_cmd=f"{stress_read_cmd} -node whitelist {node.cql_address}",
                round_robin=True,
                node_list=[node],
            )
            for node in self.db_cluster.data_nodes
        ]
        for _thread in stress_queue:
            assert self.verify_stress_thread(_thread), "Stress thread verification failed"

    def _snapshot_dataset(self) -> dict:
        """Snapshot dataset details for `cs_read_cmd_template`, from the snapshot preparer params."""
        if not (size := self.params.get("mgmt_prepare_snapshot_size")):
            return {}
        preparer_params = self.params.get("mgmt_snapshots_preparer_params")
        return {
            "num_of_rows": size * 1024 * 1024,
            **{key: preparer_params[key] for key in ("compaction", "cl", "col_size", "col_n", "replication", "rf")},
        }

    def _log_snapshot_entry(self, tag: str, prefix: str, size: int, node_ids: list[str] | None = None) -> None:
        """Log the snapshot entry to add to `defaults/manager_restore_benchmark_snapshots.yaml`."""
        tables = {keyspace: self._get_tables(keyspace) for keyspace in self.keyspaces}
        # dataset size, single replica
        dataset_size = size // self._get_rf(self.keyspaces[0])
        table_size = dataset_size // sum(len(ks_tables) for ks_tables in tables.values())
        dataset = self._snapshot_dataset()
        entry = {
            "tag": tag,
            "locations": [self.location],
            "prefix": prefix,
            "exp_timeout": HOUR_IN_SEC * 8,
            "scylla_version": self.api_node.scylla_version,
            "number_of_nodes": len(self.db_cluster.data_nodes),
            # verification read and schema creation of restore tests need the dataset from the snapshot preparer
            "prohibit_verification_read": not dataset,
            "dataset": {
                "schema": {ks: [{table: table_size} for table in ks_tables] for ks, ks_tables in tables.items()},
                **dataset,
            },
        }
        if node_ids:
            entry["node_ids"] = node_ids
        name = f"single_copy_{dataset_size}gb_{prefix.rsplit('/', 1)[-1]}_{int(time.time())}"
        self.log.info(
            "Snapshot entry for defaults/manager_restore_benchmark_snapshots.yaml (under `sizes:`):\n%s",
            textwrap.indent(yaml.safe_dump({name: entry}, sort_keys=False), "  "),
        )

    def _send_snapshot_details(self, tag: str, prefix: str, duration: int, node_ids: list[str] | None = None) -> None:
        size = self.get_keyspaces_disk_size_gb(self.keyspaces)
        self._log_snapshot_entry(tag, prefix, size, node_ids)
        snapshot_details = {
            "tag": tag,
            "size": size,
            "duration": duration,
            "locations": f"{self.location}/{prefix}",
            "ks_name": ",".join(self.keyspaces),
            "scylla_version": self.params.get_version_based_on_conf()[0],
        }
        if (size_on_bucket := self._get_prefix_size_on_bucket(prefix)) is not None:
            snapshot_details["size_on_bucket"] = size_on_bucket
        self.log.info("Send snapshot details to Argus: %s", snapshot_details)
        send_manager_snapshot_details_to_argus(
            argus_client=self.test_config.argus_client(), snapshot_details=snapshot_details
        )

    def _post_restore_repair(self) -> int:
        """Run incremental repair after restore. Return duration in seconds."""
        self.log.info("Run post restore incremental repair")
        with ExecutionTimer() as timer:
            self._repair()
        return int(timer.duration.total_seconds())

    def _send_restore_results(self, restore_time: int, repair_time: int, label: str) -> None:
        results = {
            "restore time": restore_time,
            "repair time": repair_time,
            "total": restore_time + repair_time,
            # dataset size, single replica
            "size": self.get_keyspaces_disk_size_gb(self.keyspaces) // self._get_rf(self.keyspaces[0]),
        }
        self.log.info("Send restore results to Argus: %s", results)
        send_manager_benchmark_results_to_argus(
            argus_client=self.test_config.argus_client(),
            result=results,
            result_table=ManagerRestoreBenchmarkResult(),
            row_name=label,
        )

    def _test_backup(self, repaired_percent: int) -> None:
        """Single copy backup: compare keyspace size on disk with size on bucket and measure backup duration.

        Repaired data is expected to be uploaded from one replica only, unrepaired data from every replica.
        """
        self._write_data(repaired_percent)
        snapshot_tag, prefix, duration = self._cluster_backup(name=f"backup_{repaired_percent}_repaired")
        self._send_snapshot_details(snapshot_tag, prefix, duration)

    def test_backup_unrepaired(self):
        self._test_backup(repaired_percent=0)

    def test_backup_half_repaired(self):
        self._test_backup(repaired_percent=50)

    def test_backup_85_percent_repaired(self):
        self._test_backup(repaired_percent=85)

    def test_backup_fully_repaired(self):
        self._test_backup(repaired_percent=100)

    def test_backup_old_format(self):
        """Per node (manifest `scope=node`) backup of each table of each node via `/storage_service/backup`.

        Data is not repaired, so the backup can be taken by Scylla versions without incremental repair.
        """
        self._write_data(repaired_percent=0)

        snapshot_tag = f"sct_single_copy_old_format_{int(time.time())}"
        base_prefix = self._new_prefix("old_format")
        endpoint = self._get_object_storage_endpoint()
        nodes = self.db_cluster.data_nodes
        host_ids = [node.host_id for node in nodes]
        assert None not in host_ids and len(set(host_ids)) == len(nodes), f"Unexpected host ids: {host_ids}"
        with ExecutionTimer() as timer:
            for node in nodes:
                for keyspace in self.keyspaces:
                    node.run_nodetool("snapshot", args=f"-t {snapshot_tag} {keyspace}")
            # one prefix per node and table, as each per node backup writes its own `manifest.json`
            backup_tasks = [
                (
                    node,
                    self._scylla_api(
                        "POST",
                        "storage_service/backup",
                        params={
                            "endpoint": endpoint,
                            "bucket": self.bucket,
                            "prefix": f"{base_prefix}/{node.host_id}/{keyspace}/{table}",
                            "keyspace": keyspace,
                            "table": table,
                            "snapshot": snapshot_tag,
                        },
                        node=node,
                    ),
                )
                for node in nodes
                for keyspace in self.keyspaces
                for table in self._get_tables(keyspace)
            ]
            for node, task_id in backup_tasks:
                self._assert_task_done(task_id, node=node)
        self._send_snapshot_details(snapshot_tag, base_prefix, int(timer.duration.total_seconds()), node_ids=host_ids)

    def test_restore(self):
        """Restore a single copy or a per node (`test_backup_old_format`) backup, verify every replica got the data.

        Steps:
        1. Create the schema of the snapshot.
        2. Restore all tables via `/storage_service/tablets/restore`.
        3. Run incremental repair and send restore results to Argus.
        4. Read all data with CL=ONE from each node only.
        """
        if old_format := bool(self.snapshot.node_ids):
            dcs = self.get_all_dcs_names()
            assert len(dcs) == 1, f"Per node backup restore is implemented for a single DC only, got {dcs}"
        self._create_snapshot_schema()
        restore_time = self._restore()
        repair_time = self._post_restore_repair()
        self._send_restore_results(restore_time, repair_time, label="old format" if old_format else "single copy")
        self._verify_data_on_each_node()

    def test_abort_backup(self):
        """Abort a running single copy backup, then check a new backup succeeds.

        Then compare the objects of both backups: the aborted one must have stopped uploading mid-flight.
        """
        self._write_data(repaired_percent=50)

        snapshot_tag = f"sct_single_copy_aborted_{int(time.time())}"
        aborted_prefix = self._new_prefix("aborted")
        self._abort_task_in_progress(self._start_cluster_backup(snapshot_tag, aborted_prefix))
        self._clear_snapshot(snapshot_tag)

        snapshot_tag, prefix, duration = self._cluster_backup()
        self._send_snapshot_details(snapshot_tag, prefix, duration)
        self._assert_aborted_backup_is_partial(aborted_prefix, prefix)

    def test_abort_restore(self):
        """Abort a running restore, then check a new restore resumes it and restores all data.

        Sstables already downloaded by the aborted restore are marked in `system_distributed.snapshot_sstables`
        and skipped by the next restore of the same snapshot, so tables are not truncated again.
        """
        self._create_snapshot_schema()
        task_ids = self._start_restore_all_tables()
        self._abort_task_in_progress(task_ids[0])
        for task_id in task_ids[1:]:
            self._wait_for_task(task_id, timeout=self.snapshot.exp_timeout)

        restore_time = self._restore()
        repair_time = self._post_restore_repair()
        self._send_restore_results(restore_time, repair_time, label="after abort")
        self._verify_data_on_each_node()
