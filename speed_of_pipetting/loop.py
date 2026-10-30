"""Schedule one week of plates under three lab policies.

A plate is reserved in full before it starts. A failure stops the plate and
releases the unrun tail. The owned lab keeps that failure as a training record
and may spend one corrected rerun. The churn policy does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

POLICIES = ("human-loop", "churn-on-fail", "owned-lab")


@dataclass
class JobRow:
    id: str
    status: str
    failure: str | None
    record: bool
    reagent_usd: int
    revenue_usd: int


@dataclass
class PolicyReport:
    policy: str
    jobs: list[JobRow] = field(default_factory=list)
    remaining_robot: int = 0
    remaining_tech: int = 0

    @property
    def shipped(self) -> int:
        return sum(1 for job in self.jobs if job.status in {"ship", "recovered"})

    @property
    def recovered(self) -> int:
        return sum(1 for job in self.jobs if job.status == "recovered")

    @property
    def records(self) -> int:
        return sum(1 for job in self.jobs if job.record)

    @property
    def churned(self) -> int:
        return sum(1 for job in self.jobs if job.status == "churned")

    @property
    def deferred(self) -> int:
        return sum(1 for job in self.jobs if job.status == "defer")

    @property
    def reagent_usd(self) -> int:
        return sum(job.reagent_usd for job in self.jobs)

    @property
    def revenue_usd(self) -> int:
        return sum(job.revenue_usd for job in self.jobs)

    @property
    def margin_usd(self) -> int:
        return self.revenue_usd - self.reagent_usd


def load_week(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("week file must be a JSON object")
    _validate(data)
    return data


def run_week(data: dict) -> list[PolicyReport]:
    _validate(data)
    return [_run_policy(data, policy) for policy in POLICIES]


def format_table(data: dict, reports: list[PolicyReport]) -> str:
    lab = data["lab"]
    lines = [
        (
            f"week {data['week']}  robot {lab['robot_minutes']} min  "
            f"tech {lab['tech_minutes']} min  jobs {len(data['proposals'])}"
        ),
        "",
    ]
    headers = [
        "policy",
        "shipped",
        "recovered",
        "records",
        "churned",
        "deferred",
        "reagent USD",
        "revenue USD",
        "margin USD",
    ]
    rows = [
        [
            report.policy,
            str(report.shipped),
            str(report.recovered),
            str(report.records),
            str(report.churned),
            str(report.deferred),
            str(report.reagent_usd),
            str(report.revenue_usd),
            str(report.margin_usd),
        ]
        for report in reports
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    lines.append(_format_row(headers, widths))
    lines.append(_format_row(["-" * width for width in widths], widths))
    for row in rows:
        lines.append(_format_row(row, widths, numeric=True))
    lines.append("")
    lines.append(format_result_line(reports))
    return "\n".join(lines)


def format_details(reports: list[PolicyReport]) -> str:
    blocks: list[str] = []
    for report in reports:
        blocks.append(report.policy)
        for job in report.jobs:
            failure = f"  {job.failure}" if job.failure else ""
            record = "  record" if job.record else ""
            blocks.append(
                f"  {job.id:<12} {job.status:<9}{failure:<22}{record:<8} "
                f"reagent {job.reagent_usd:<6} revenue {job.revenue_usd}"
            )
        blocks.append(
            f"  remaining robot {report.remaining_robot} min  "
            f"tech {report.remaining_tech} min"
        )
        blocks.append("")
    return "\n".join(blocks).rstrip()


def format_result_line(reports: list[PolicyReport]) -> str:
    parts = [
        (
            f"{report.policy} ships {report.shipped} packages "
            f"({report.recovered} recovered) on margin USD {report.margin_usd}"
        )
        for report in reports
    ]
    return "; ".join(parts) + "."


def to_json(data: dict, reports: list[PolicyReport]) -> str:
    payload = {
        "week": data["week"],
        "result": format_result_line(reports),
        "policies": [
            {
                "policy": report.policy,
                "shipped": report.shipped,
                "recovered": report.recovered,
                "records": report.records,
                "churned": report.churned,
                "deferred": report.deferred,
                "reagent_usd": report.reagent_usd,
                "revenue_usd": report.revenue_usd,
                "margin_usd": report.margin_usd,
                "remaining_robot": report.remaining_robot,
                "remaining_tech": report.remaining_tech,
                "jobs": [
                    {
                        "id": job.id,
                        "status": job.status,
                        "failure": job.failure,
                        "record": job.record,
                        "reagent_usd": job.reagent_usd,
                        "revenue_usd": job.revenue_usd,
                    }
                    for job in report.jobs
                ],
            }
            for report in reports
        ],
    }
    return json.dumps(payload, indent=2)


def _run_policy(data: dict, policy: str) -> PolicyReport:
    cap = {
        "robot": data["lab"]["robot_minutes"],
        "tech": data["lab"]["tech_minutes"],
    }
    recoverable = set(data["recoverable"])
    report = PolicyReport(policy=policy)
    for proposal in data["proposals"]:
        if not _reserve(cap, proposal, policy):
            report.jobs.append(
                JobRow(proposal["id"], "defer", None, False, 0, 0)
            )
            continue
        ran, failure = _first_pass(proposal)
        _release_tail(cap, proposal, ran, policy)
        reagent = proposal["reagent_usd"]
        if failure is None:
            report.jobs.append(
                JobRow(
                    proposal["id"],
                    "ship",
                    None,
                    False,
                    reagent,
                    proposal["package_usd"],
                )
            )
            continue
        if policy == "churn-on-fail":
            report.jobs.append(
                JobRow(proposal["id"], "churned", failure, False, reagent, 0)
            )
            continue
        if failure in recoverable and proposal.get("rerun") == "pass":
            if _reserve(cap, proposal, policy):
                _release_tail(cap, proposal, proposal["steps"], policy)
                report.jobs.append(
                    JobRow(
                        proposal["id"],
                        "recovered",
                        failure,
                        True,
                        reagent + proposal["reagent_usd"],
                        proposal["package_usd"],
                    )
                )
                continue
        report.jobs.append(
            JobRow(proposal["id"], "recorded", failure, True, reagent, 0)
        )
    report.remaining_robot = cap["robot"]
    report.remaining_tech = cap["tech"]
    return report


def _first_pass(proposal: dict) -> tuple[list[dict], str | None]:
    ran: list[dict] = []
    for step in proposal["steps"]:
        ran.append(step)
        if step["outcome"] == "fail":
            return ran, step["failure"]
    return ran, None


def _pool(step: dict, policy: str) -> str:
    if policy == "human-loop" or step["kind"] == "judgment":
        return "tech"
    return "robot"


def _need(steps: list[dict], policy: str) -> dict[str, int]:
    need = {"robot": 0, "tech": 0}
    for step in steps:
        need[_pool(step, policy)] += step["minutes"]
    return need


def _reserve(cap: dict[str, int], proposal: dict, policy: str) -> bool:
    need = _need(proposal["steps"], policy)
    if cap["robot"] < need["robot"] or cap["tech"] < need["tech"]:
        return False
    cap["robot"] -= need["robot"]
    cap["tech"] -= need["tech"]
    return True


def _release_tail(
    cap: dict[str, int], proposal: dict, ran: list[dict], policy: str
) -> None:
    reserved = _need(proposal["steps"], policy)
    used = _need(ran, policy)
    cap["robot"] += reserved["robot"] - used["robot"]
    cap["tech"] += reserved["tech"] - used["tech"]


def _format_row(cells: list[str], widths: list[int], numeric: bool = False) -> str:
    parts = []
    for index, cell in enumerate(cells):
        if numeric and index > 0:
            parts.append(cell.rjust(widths[index]))
        else:
            parts.append(cell.ljust(widths[index]))
    return "  ".join(parts)


def _validate(data: dict) -> None:
    week = data.get("week")
    if not isinstance(week, str) or not week.strip():
        raise ValueError("week must be a non-empty string")
    lab = data.get("lab")
    if not isinstance(lab, dict):
        raise ValueError("lab must be an object")
    _pos_int(lab.get("robot_minutes"), "robot_minutes", zero=False)
    _pos_int(lab.get("tech_minutes"), "tech_minutes", zero=False)
    recoverable = data.get("recoverable")
    if not isinstance(recoverable, list) or not all(
        isinstance(item, str) and item for item in recoverable
    ):
        raise ValueError("recoverable must be a list of strings")
    proposals = data.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        raise ValueError("proposals must be a non-empty list")
    seen: set[str] = set()
    for proposal in proposals:
        _validate_proposal(proposal, seen)


def _validate_proposal(proposal: dict, seen: set[str]) -> None:
    if not isinstance(proposal, dict):
        raise ValueError("each proposal must be an object")
    ident = proposal.get("id")
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError("proposal id must be a non-empty string")
    if ident in seen:
        raise ValueError(f"duplicate proposal id: {ident}")
    seen.add(ident)
    title = proposal.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{ident}: title must be a non-empty string")
    _pos_int(proposal.get("reagent_usd"), f"{ident} reagent_usd", zero=True)
    _pos_int(proposal.get("package_usd"), f"{ident} package_usd", zero=True)
    rerun = proposal.get("rerun", "pass")
    if "rerun" in proposal and rerun != "pass":
        raise ValueError(f"{ident}: rerun must be 'pass' when set")
    steps = proposal.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{ident}: steps must be a non-empty list")
    for step in steps:
        _validate_step(ident, step)


def _validate_step(ident: str, step: dict) -> None:
    if not isinstance(step, dict):
        raise ValueError(f"{ident}: each step must be an object")
    name = step.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{ident}: step name must be a non-empty string")
    if step.get("kind") not in {"handler", "judgment"}:
        raise ValueError(f"{ident}: step kind must be handler or judgment")
    _pos_int(step.get("minutes"), f"{ident} {name} minutes", zero=False)
    outcome = step.get("outcome")
    if outcome not in {"pass", "fail"}:
        raise ValueError(f"{ident}: step outcome must be pass or fail")
    if outcome == "fail":
        failure = step.get("failure")
        if not isinstance(failure, str) or not failure.strip():
            raise ValueError(f"{ident}: a failed step needs a failure class")


def _pos_int(value: object, label: str, zero: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 0 or (value == 0 and not zero):
        raise ValueError(f"{label} must be a positive integer")
