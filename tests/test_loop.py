"""The sample week is the contract. Counts are checked against a hand trace."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from speed_of_pipetting.loop import format_result_line, load_week, run_week

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "week.json"


class WeekTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load_week(FIXTURE)
        self.reports = {report.policy: report for report in run_week(self.data)}

    def test_owned_lab_keeps_failures_and_ships_more(self) -> None:
        owned = self.reports["owned-lab"]
        churn = self.reports["churn-on-fail"]
        human = self.reports["human-loop"]
        self.assertEqual(
            (owned.shipped, owned.recovered, owned.records, owned.churned, owned.deferred),
            (5, 2, 3, 0, 1),
        )
        self.assertEqual((owned.reagent_usd, owned.revenue_usd, owned.margin_usd), (5750, 12500, 6750))
        self.assertEqual((owned.remaining_robot, owned.remaining_tech), (40, 165))
        self.assertEqual(
            (churn.shipped, churn.recovered, churn.records, churn.churned, churn.deferred),
            (3, 0, 0, 4, 0),
        )
        self.assertEqual((churn.reagent_usd, churn.revenue_usd, churn.margin_usd), (4750, 6000, 1250))
        self.assertEqual((churn.remaining_robot, churn.remaining_tech), (220, 220))
        self.assertEqual(
            (human.shipped, human.recovered, human.records, human.churned, human.deferred),
            (2, 0, 1, 0, 4),
        )
        self.assertEqual((human.reagent_usd, human.revenue_usd, human.margin_usd), (1650, 3800, 2150))
        self.assertEqual((human.remaining_robot, human.remaining_tech), (1000, 30))
        self.assertGreater(owned.margin_usd, human.margin_usd)
        self.assertGreater(human.margin_usd, churn.margin_usd)

    def test_non_recoverable_failure_is_not_rerun(self) -> None:
        jobs = {job.id: job for job in self.reports["owned-lab"].jobs}
        self.assertEqual(jobs["screen-c"].status, "recorded")
        self.assertTrue(jobs["screen-c"].record)
        self.assertEqual(jobs["screen-c"].failure, "wrong-reagent-lot")
        self.assertEqual(jobs["screen-b"].status, "recovered")
        self.assertEqual(jobs["screen-f"].status, "defer")

    def test_result_line(self) -> None:
        line = format_result_line(list(self.reports.values()))
        self.assertEqual(
            line,
            "human-loop ships 2 packages (0 recovered) on margin USD 2150; "
            "churn-on-fail ships 3 packages (0 recovered) on margin USD 1250; "
            "owned-lab ships 5 packages (2 recovered) on margin USD 6750.",
        )

    def test_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "speed_of_pipetting", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("owned-lab ships 5 packages (2 recovered) on margin USD 6750.", proc.stdout)

    def test_cli_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "speed_of_pipetting", "--json", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["policies"][2]["policy"], "owned-lab")
        self.assertEqual(payload["policies"][2]["margin_usd"], 6750)

    def test_bad_fixture(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_week(ROOT / "fixtures" / "missing.json")
        bad = json.loads(FIXTURE.read_text(encoding="utf-8"))
        bad["proposals"][1]["steps"][1].pop("failure")
        with self.assertRaises(ValueError):
            run_week(bad)


if __name__ == "__main__":
    unittest.main()
