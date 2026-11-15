"""离线门户演练：映射审阅、计划确认、版本变化衔接。

全程只操作本地合成资料。``preflight``/``review`` 一步不提交；门户提交需要
显式调用 :func:`submit`，且会先用当前表单重新校验计划。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .forms import FormVersion, diff_forms
from .impact import Draft, DraftEntry, analyse_impact
from .ledger import Ledger
from .mapping import MaterialFile, review_mapping
from .plan import SubmissionPlan, build_plan, confirm_plan, portal_payload, preflight
from .rehearsal import (
    application_from_scenario,
    files_from_scenario,
    forms_from_scenario,
    load_scenario,
)


@dataclass(frozen=True)
class Rehearsal:
    scenario: dict[str, Any]
    forms: dict[str, FormVersion]
    ledger: Ledger

    @classmethod
    def load(cls, path: Path | None = None, ledger: Ledger | None = None) -> "Rehearsal":
        from .rehearsal import load_ledger

        data = load_scenario(path)
        if ledger is None:
            return cls(data, forms_from_scenario(data), load_ledger(data))
        # 传入的记录可能来自持久化快照；只用场景补齐其中还没有的决定，
        # 已有的记录保持不变。
        load_ledger(data, ledger=ledger)
        return cls(data, forms_from_scenario(data), ledger)

    def application(self, application_id: str) -> dict[str, Any]:
        return application_from_scenario(self.scenario, application_id)

    def form_for(self, application_id: str) -> FormVersion:
        application = self.application(application_id)
        key = "|".join(
            (application["district"], application["application_type"], application["form_version"])
        )
        if key not in self.forms:
            raise KeyError("form version not in scenario: " + key)
        return self.forms[key]

    def files(self, application_id: str) -> tuple[MaterialFile, ...]:
        return files_from_scenario(self.scenario, application_id)

    def review_object(self, application_id: str):
        return review_mapping(
            self.form_for(application_id),
            self.files(application_id),
            self.ledger.rules(),
            self.ledger.case_decisions(),
            application_id,
        )

    def review(self, application_id: str) -> dict[str, Any]:
        return self.review_object(application_id).as_dict()

    def plan(self, application_id: str) -> SubmissionPlan:
        return build_plan(self.review_object(application_id))

    def drafts(self) -> tuple[Draft, ...]:
        result = []
        for raw in self.scenario.get("drafts", ()):
            result.append(
                Draft(
                    draft_id=raw["draft_id"],
                    application_id=raw["application_id"],
                    form_key=raw["form_key"],
                    entries=tuple(
                        DraftEntry(item["field"], item["file_id"], item.get("placement", "primary"))
                        for item in raw.get("entries", ())
                    ),
                    prepared_at=raw.get("prepared_at", ""),
                )
            )
        return tuple(result)

    def impact(self, before_key: str, after_key: str) -> dict[str, Any]:
        before = self.forms[before_key]
        after = self.forms[after_key]
        plans = []
        for application in self.scenario.get("applications", ()):
            plans.append(self.plan(application["application_id"]))
        files = [
            file
            for application in self.scenario.get("applications", ())
            for file in self.files(application["application_id"])
        ]
        report = analyse_impact(
            before,
            after,
            rules=self.ledger.all_rule_versions(),
            plans=plans,
            drafts=self.drafts(),
            case_decisions=self.ledger.case_decisions(),
            ledger=self.ledger,
            files=files,
        )
        return report.as_dict()


def confirm(rehearsal: Rehearsal, application_id: str, actor: str) -> dict[str, Any]:
    plan = rehearsal.plan(application_id)
    confirmed = confirm_plan(plan, actor)
    return confirmed.as_dict()


def preflight_report(rehearsal: Rehearsal, plan: SubmissionPlan, current_key: str) -> dict[str, Any]:
    current = rehearsal.forms[current_key]
    checked = preflight(plan, current)
    payload = portal_payload(plan, current, file_by_id={
        file.file_id: file for file in rehearsal.files(plan.application_id)
    })
    return {"plan": checked.as_dict(), "portal": payload}


def submit(rehearsal: Rehearsal, plan: SubmissionPlan, current_key: str, base_url: str, token: str) -> dict[str, Any]:
    """显式提交到本地演练门户；失效计划会被拒绝，不会发出请求。"""
    current = rehearsal.forms[current_key]
    payload = portal_payload(
        plan,
        current,
        csrf=token,
        file_by_id={file.file_id: file for file in rehearsal.files(plan.application_id)},
    )
    if not payload["executable"]:
        return {"submitted": False, "portal": payload}
    body = urlencode(
        {"id": plan.application_id, "csrf": token, **payload["fields"]}
    ).encode()
    request = Request(base_url.rstrip("/") + "/portal/submit", data=body)
    try:
        with urlopen(request, timeout=3) as response:
            reply = json.loads(response.read())
    except (URLError, OSError) as error:
        return {"submitted": False, "error": str(error), "portal": payload}
    return {"submitted": True, "response": reply, "portal": payload}


def diff_summary(before: FormVersion, after: FormVersion) -> dict[str, Any]:
    return diff_forms(before, after).as_dict()


def run_rehearsal(
    application_id: str,
    path: Path | None = None,
    actor: str = "申报员-演练",
    after_key: str | None = None,
) -> dict[str, Any]:
    """在本地合成资料上跑完整流程，返回可审阅、可打印的步骤记录。

    步骤顺序：通读所有申请 → 确认一笔计划 → 用同一计划对照新版本。
    全程不提交；``portal`` 字段只是将要发送的数据。
    """
    rehearsal = Rehearsal.load(path)
    try:
        applications = [item["application_id"] for item in rehearsal.scenario["applications"]]
        if application_id not in applications:
            raise KeyError(application_id)
        reviews = []
        for item in applications:
            review = rehearsal.review(item)
            reviews.append(
                {
                    "application_id": item,
                    "scope": review["scope"],
                    "status": review["status"],
                    "missing_required": review["missing_required"],
                    "unassigned_files": review["unassigned_files"],
                    "files": [
                        {
                            "file_id": mapping["file"]["file_id"],
                            "status": mapping["status"],
                            "targets": [
                                {
                                    "field": suggestion["field"],
                                    "placement": suggestion["placement"],
                                    "origin": suggestion["origin"],
                                }
                                for suggestion in mapping["suggestions"]
                            ],
                            "reason": mapping.get("reason"),
                        }
                        for mapping in review["files"]
                    ],
                    "notes": review["notes"],
                    "not_applied_rules": review["not_applied_rules"],
                    "not_applied_decisions": review["not_applied_decisions"],
                }
            )

        plan = rehearsal.plan(application_id)
        confirmed = None
        if plan.status == "ready_for_review":
            confirmed = confirm_plan(plan, actor)

        stages = {"reviews": reviews, "plan": plan.as_dict()}
        if confirmed is None:
            stages["confirmation"] = {
                "confirmed": False,
                "reason": "计划存在阻断项，未进入确认。",
                "blockers": [item for item in plan.blockers],
            }
            return stages

        stages["confirmation"] = confirmed.as_dict()
        target_key = after_key or confirmed.form.key
        stages["preflight"] = preflight_report(rehearsal, confirmed, target_key)
        stages["preflight"]["note"] = "预检只计算提交数据，不发送请求。"
        if target_key != confirmed.form.key:
            stages["impact"] = rehearsal.impact(confirmed.form.key, target_key)
            # 失效判定必须来自计划本身，而不是只靠调用方查看差异
            stages["preflight"]["plan_invalid"] = (
                stages["preflight"]["plan"].get("invalidation") is not None
            )
        return stages
    finally:
        rehearsal.ledger.close()
