from time import sleep
from longevity_test import LongevityTest
from sdcm.sct_events import Severity
from sdcm.sct_events.filters import EventsSeverityChangerFilter
from sdcm.sct_events.loaders import CassandraStressEvent


class TruncateTest(LongevityTest):
    def update_ks_name(self, i):
        self.params["prepare_write_cmd"] = [cmd.replace(
            "ks_name", f"ks_{i}") for cmd in self.params["prepare_write_cmd"]]
        self.params["prepare_write_cmd"] = [cmd.replace(
            f"ks_{i-1}", f"ks_{i}") for cmd in self.params["prepare_write_cmd"]]
        self.params["post_prepare_cql_cmds"] = [cmd.replace(
            "ks_name", f"ks_{i}") for cmd in self.params["post_prepare_cql_cmds"]]
        self.params["post_prepare_cql_cmds"] = [cmd.replace(
            f"ks_{i-1}", f"ks_{i}") for cmd in self.params["post_prepare_cql_cmds"]]

    def test_100_truncates(self):
        for i in range(100):
            self.update_ks_name(i)
            self.run_prepare_write_cmd()
            sleep(60)

        with EventsSeverityChangerFilter(new_severity=Severity.NORMAL, event_class=CassandraStressEvent, extra_time_to_expiration=60):
            self.loaders.kill_stress_thread()
