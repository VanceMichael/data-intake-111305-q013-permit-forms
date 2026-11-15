"""表单版本变化对既有映射、规则、计划和草稿的影响。

原则：

* 不自动迁移。版本变化后，旧规则、旧计划、旧草稿一律保持原样并标记为
  ``needs_review``，由人工决定如何处理。
* 影响范围依据 :func:`permit_forms.forms.diff_forms` 的结构差异计算，
  不使用字段名相似度。
* 原文件、项目内例外和规则事件都保留引用，审阅时可直接回查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .forms import FormDiff, FormVersion, diff_forms
from .ledger import Ledger
from .mapping import CaseDecision, MappingRule, MaterialFile
from .plan import SubmissionPlan


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class DraftEntry:
    field: str
    file_id: str
    placement: str = "primary"

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "file_id": self.file_id, "placement": self.placement}


@dataclass(frozen=True)
class Draft:
    """已经准备但尚未提交的草稿。只能是某一份申请、某一个表单版本。"""

    draft_id: str
    application_id: str
    form_key: str
    entries: tuple[DraftEntry, ...]
    prepared_at: str = ""

    def __post_init__(self):
        if not self.draft_id.strip():
            raise ValueError("draft_id must be non-empty")
        if not self.application_id.strip():
            raise ValueError("draft application_id must be non-empty")
        if not self.form_key.strip():
            raise ValueError("draft form_key must be non-empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "application_id": self.application_id,
            "form_key": self.form_key,
            "prepared_at": self.prepared_at,
            "entries": [item.as_dict() for item in self.entries],
        }


@dataclass(frozen=True)
class FieldImpact:
    field: str
    change_kind: str
    related: tuple[str, ...]
    affected_rules: tuple[str, ...]
    affected_plans: tuple[str, ...]
    affected_drafts: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "change_kind": self.change_kind,
            "related": list(self.related),
            "affected_rules": list(self.affected_rules),
            "affected_plans": list(self.affected_plans),
            "affected_drafts": list(self.affected_drafts),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ImpactReport:
    from_form: str
    to_form: str
    diff: FormDiff
    impacts: tuple[FieldImpact, ...]
    affected_rules: tuple[dict[str, Any], ...]
    affected_plans: tuple[dict[str, Any], ...]
    affected_drafts: tuple[dict[str, Any], ...]
    preserved: dict[str, Any]
    generated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_form,
            "to": self.to_form,
            "diff": self.diff.as_dict(),
            "impacts": [item.as_dict() for item in self.impacts],
            "affected_rules": [dict(item) for item in self.affected_rules],
            "affected_plans": [dict(item) for item in self.affected_plans],
            "affected_drafts": [dict(item) for item in self.affected_drafts],
            "preserved": dict(self.preserved),
            "generated_at": self.generated_at,
        }


def analyse_impact(
    before: FormVersion,
    after: FormVersion,
    rules: Iterable[MappingRule] = (),
    plans: Iterable[SubmissionPlan] = (),
    drafts: Iterable[Draft] = (),
    case_decisions: Iterable[CaseDecision] = (),
    ledger: Ledger | None = None,
    files: Iterable[MaterialFile] = (),
) -> ImpactReport:
    if not isinstance(before, FormVersion) or not isinstance(after, FormVersion):
        raise TypeError("before and after must be FormVersion")
    diff = diff_forms(before, after)
    changed = diff.affected_fields()

    # 规则只在其作用域与旧表单完全一致时纳入影响判断；其他版本的历史规则
    # 本来就不该套用，不出现在这里。
    candidate_rules = [
        rule
        for rule in rules
        if rule.scope.key == before.key and rule.target_field in changed
    ]
    rule_ids = {rule.rule_id for rule in candidate_rules}

    candidate_plans = []
    for plan in plans:
        if plan.form.key != before.key:
            continue
        if plan.invalidated_at is not None:
            candidate_plans.append((plan, "已失效"))
            continue
        if any(entry.field in changed for entry in plan.entries):
            candidate_plans.append((plan, "引用了变化字段"))

    candidate_drafts = [
        draft for draft in drafts if draft.form_key == before.key and any(entry.field in changed for entry in draft.entries)
    ]

    impacts = []
    for change in diff.changes:
        related = set(change.related)
        field_rules = tuple(
            sorted(
                rule.rule_id
                for rule in candidate_rules
                if rule.target_field == change.field or rule.target_field in related
            )
        )
        field_plans = tuple(
            sorted(
                plan.plan_id
                for plan, _ in candidate_plans
                if any(entry.field == change.field or entry.field in related for entry in plan.entries)
            )
        )
        field_drafts = tuple(
            sorted(
                draft.draft_id
                for draft in candidate_drafts
                if any(entry.field == change.field or entry.field in related for entry in draft.entries)
            )
        )
        impacts.append(
            FieldImpact(
                field=change.field,
                change_kind=change.kind,
                related=change.related,
                affected_rules=field_rules,
                affected_plans=field_plans,
                affected_drafts=field_drafts,
                detail=change.detail,
            )
        )

    affected_plans_payload = []
    for plan, why in sorted(candidate_plans, key=lambda item: item[0].plan_id):
        affected_plans_payload.append(
            {
                "plan_id": plan.plan_id,
                "application_id": plan.application_id,
                "status": plan.status,
                "confirmed": plan.confirm_token is not None,
                "reason": why,
                "entries": [item.as_dict() for item in plan.entries],
                "action": "重新生成计划并再次确认；原计划保留备查",
            }
        )

    rules_payload = []
    for rule in sorted(candidate_rules, key=lambda item: item.rule_id):
        rules_payload.append(
            {
                **rule.as_dict(),
                "reason": "规则目标字段在版本变化中受影响，未自动迁移",
                "history": [
                    event.as_dict()
                    for event in (ledger.history(rule.rule_id) if ledger is not None else ())
                ],
            }
        )

    drafts_payload = []
    for draft in sorted(candidate_drafts, key=lambda item: item.draft_id):
        entries = []
        for entry in draft.entries:
            if entry.field in changed:
                entries.append({**entry.as_dict(), "status": "needs_review"})
            else:
                entries.append({**entry.as_dict(), "status": "unaffected"})
        drafts_payload.append(
            {
                **draft.as_dict(),
                "status": "needs_review",
                "entries": entries,
                "action": "草稿不自动迁移；需按新版本逐项确认",
            }
        )

    preserved = {
        "files": [item.as_dict() for item in files if _file_referenced(item, candidate_rules, candidate_plans, candidate_drafts)],
        "case_decisions": [item.as_dict() for item in case_decisions if item.scope.key == before.key],
        "ledger_events": [
            event.as_dict()
            for event in (ledger.events() if ledger is not None else ())
            if event.kind != "rule_deactivated"
        ],
        "rule_versions": [
            rule.as_dict()
            for rule in (ledger.all_rule_versions() if ledger is not None else candidate_rules)
            if rule.scope.key == before.key or rule.rule_id in rule_ids
        ],
        "plan_snapshots": [plan.as_dict() for plan, _ in candidate_plans],
        "draft_snapshots": [draft.as_dict() for draft in candidate_drafts],
        "note": "原文件、项目内例外和裁定事件均保留；审阅只增加标记，不删除历史。",
    }

    return ImpactReport(
        from_form=before.key,
        to_form=after.key,
        diff=diff,
        impacts=tuple(impacts),
        affected_rules=tuple(rules_payload),
        affected_plans=tuple(affected_plans_payload),
        affected_drafts=tuple(drafts_payload),
        preserved=preserved,
        generated_at=_now(),
    )


def _file_referenced(file: MaterialFile, rules, plans, drafts) -> bool:
    for draft in drafts:
        if any(entry.file_id == file.file_id for entry in draft.entries):
            return True
    for plan, _ in plans:
        if any(entry.file_id == file.file_id for entry in plan.entries):
            return True
    return False


def suggestions_needing_attention(review_files) -> list[dict[str, Any]]:
    """审阅输出中需要人工关注的项目，供命令行直接展示。"""
    result = []
    for mapping in review_files:
        if mapping.status == "insufficient_evidence":
            result.append(
                {
                    "file_id": mapping.file.file_id,
                    "status": mapping.status,
                    "reason": mapping.reason,
                }
            )
        elif mapping.status == "excluded_by_case_decision":
            result.append(
                {
                    "file_id": mapping.file.file_id,
                    "status": mapping.status,
                    "decision_id": mapping.case_decision_id,
                }
            )
    return result
