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

import logging

import pytest

from sdcm.db_log_reader import DbLogReader


@pytest.mark.parametrize(
    "line, echoed",
    [
        pytest.param(
            "2026-10-27T11:33:43.447 db-node-2  !INFO | scylla[16038]  [shard 0:strm] repair - Skipped adding "
            "sst=/var/lib/scylla/data/ks/t-1/me-3gr2_0q1a_1-big-Data.db for incremental repair",
            False,
            id="vector-info-repair",
        ),
        pytest.param(
            "2026-10-27T11:33:43.548+00:00 db-node-2     !INFO | scylla[16038]:  [shard 0: gms] raft_topology - "
            "Got raft_topology_cmd::barrier_and_drain, version 21577",
            False,
            id="syslogng-info-barrier",
        ),
        pytest.param(
            "2026-10-27T11:33:43.461 db-node-2  !WARNING | scylla[16038]  [shard 0:strm] repair - "
            "repair[b5ea80cc-d9b5-494d-be64-828705d8add5]: Repair tablet for table=ks.t range=(1,2] status=failed",
            True,
            id="vector-warning-repair",
        ),
        pytest.param(
            "2026-10-27T11:33:43.461 db-node-2  !INFO | scylla[16038]  [shard 0:main] database - Flushing non-system tables",
            True,
            id="vector-info-other-logger",
        ),
        pytest.param(
            "2026-10-27T11:33:43.461 db-node-2  !WARNING | scylla[16038]  [shard 0:main] storage_proxy - "
            "Exception when communicating with 10.0.0.1",
            False,
            id="vector-warning-excluded-at-any-level",
        ),
    ],
)
def test_db_log_reader_echoes_line_to_sct_log(tmp_path, caplog, line, echoed):
    """Per-sstable repair and barrier lines filled the runner disk via sct.log; their warnings must still reach it."""
    system_log = tmp_path / "system.log"
    system_log.write_text(line + "\n")
    reader = DbLogReader(
        system_log=str(system_log),
        remoter=None,
        node_name="db-node-2",
        system_event_patterns=[],
        decoding_queue=None,
        log_lines=True,
    )

    with caplog.at_level(logging.DEBUG, logger="sdcm.db_log_reader"):
        reader._read_and_publish_events()

    assert (line in caplog.messages) is echoed
