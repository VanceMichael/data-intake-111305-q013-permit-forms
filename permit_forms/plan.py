"""填报计划、确认与执行前检查。

流程刻意分成三步，任何一步都不会触发提交：

1. :func:`build_plan` 依据映射审阅生成计划；必填字段没有材料时状态为
   ``blocked``，此时根本不能确认。
2. :func:`confirm_plan` 由申报员确认，确认令牌绑定表单版本指纹和计划内容。
3. :func:`preflight` 在执行前重新核对当前表单与计划引用的字段；版本或
   字段结构变化会使计划失效，必须重新生成并再次确认。

:func:`portal_payload` 只生成门户提交所需的数据，不发起请求。执行提交
始终需要调用方另行显式操作。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .forms import FormVersion
from .mapping import MappingReview, MaterialFile
from .util import non_empty, digest_text


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class PlanEntry:
    field: str
    label: str
    required: bool
    file_id: str
    placement: str
    origin: str
    rule_id: str | None = None
    source_decision_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "field": self.field,
            "label": self.label,
            "required": self.required,
            "file_id": self.file_id,
            "placement": self.placement,
            "origin": self.origin,
        }
        if self.rule_id:
            result["rule_id"] = self.rule_id
        if self.source_decision_id:
            result["source_decision_id"] = self.source_decision_id
        return result


@dataclass(frozen=True)
class SubmissionPlan:
    plan_id: str
    application_id: str
    form: FormVersion
    status: str  # blocked / ready_for_review
    entries: tuple[PlanEntry, ...]
    blockers: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    created_at: str
    confirmed_at: str | None = None
    confirmed_by: str | None = None
    confirm_token: str | None = None
    invalidated_at: str | None = None
    invalidation: dict[str, Any] | None = None

    @property
    def executable(self) -> bool:
        return (
            self.status == "ready_for_review"
            and self.confirm_token is not None
            and self.invalidated_at is None
            and not self.blockers
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "plan_id": self.plan_id,
            "application_id": self.application_id,
            "form": self.form.as_dict(),
            "form_fingerprint": self.form.fingerprint(),
            "status": self.status,
            "executable": self.executable,
            "entries": [item.as_dict() for item in self.entries],
            "blockers": [dict(item) for item in self.blockers],
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "created_at": self.created_at,
        }
        if self.confirmed_at:
            result["confirmed_at"] = self.confirmed_at
            result["confirmed_by"] = self.confirmed_by
        if self.confirm_token:
            result["confirm_token"] = self.confirm_token
        if self.invalidated_at:
            result["invalidated_at"] = self.invalidated_at
            result["invalidation"] = dict(self.invalidation or {})
        return result


def build_plan(review: MappingReview) -> SubmissionPlan:
    if not isinstance(review, MappingReview):
        raise TypeError("review must be a MappingReview")
    if not review.application_id:
        raise ValueError("plan requires an application_id")
    entries: list[PlanEntry] = []
    warnings: list[str] = []
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for mapping in review.files:
        if mapping.status == "excluded_by_case_decision":
            warnings.append(
                "文件 "
                + mapping.file.file_id
                + " 被项目内例外排除，不进入计划。"
            )
            continue
        if mapping.status == "conflicting":
            blockers.append(
                {
                    "code": "conflicting_rules",
                    "file_id": mapping.file.file_id,
                    "detail": "同一材料命中多条目标不一致的规则，需人工裁定后再生成计划。",
                    "candidates": list((mapping.reason or {}).get("candidates", ())),
                }
            )
            continue
        if mapping.status != "suggested":
            warnings.append(
                "文件 "
                + mapping.file.file_id
                + " 证据不足，未进入计划："
                + str((mapping.reason or {}).get("detail", ""))
            )
            continue
        for suggestion in mapping.suggestions:
            marker = (suggestion.field, mapping.file.file_id)
            if marker in seen:
                continue
            seen.add(marker)
            field = review.form.field(suggestion.field)
            entries.append(
                PlanEntry(
                    field=suggestion.field,
                    label=field.label,
                    required=field.required,
                    file_id=mapping.file.file_id,
                    placement=suggestion.placement,
                    origin=suggestion.origin,
                    rule_id=suggestion.rule_id,
                    source_decision_id=suggestion.source_decision_id,
                )
            )
    for name in review.missing_required:
        blockers.append(
            {
                "code": "required_field_unmapped",
                "field": name,
                "detail": "必填字段没有材料，不能提交。",
            }
        )
    for mapping in review.files:
        if mapping.status == "insufficient_evidence":
            blockers.append(
                {
                    "code": "file_without_destination",
                    "file_id": mapping.file.file_id,
                    "detail": "文件尚未确定去处；需人工裁定或补充材料类型。",
                }
            )
    status = "blocked" if blockers else "ready_for_review"
    body = {
        "application_id": review.application_id,
        "form": review.form.key,
        "form_fingerprint": review.form.fingerprint(),
        "entries": [item.as_dict() for item in entries],
    }
    return SubmissionPlan(
        plan_id="plan-" + digest_text(body)[:16],
        application_id=review.application_id,
        form=review.form,
        status=status,
        entries=tuple(entries),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        notes=review.notes,
        created_at=_now(),
    )


def confirm_plan(plan: SubmissionPlan, actor: str) -> SubmissionPlan:
    """申报员确认计划；不满足条件时拒绝确认。"""
    non_empty(actor, "actor")
    if plan.status != "ready_for_review":
        raise ValueError("plan is blocked and cannot be confirmed")
    if plan.blockers:
        raise ValueError("plan has blockers and cannot be confirmed")
    token = "confirm-" + digest_text(
        {
            "plan_id": plan.plan_id,
            "form_fingerprint": plan.form.fingerprint(),
            "entries": [item.as_dict() for item in plan.entries],
        }
    )[:24]
    return replace(
        plan,
        confirmed_at=_now(),
        confirmed_by=actor,
        confirm_token=token,
    )


def current_form_fingerprint(form: FormVersion) -> str:
    return form.fingerprint()


def preflight(plan: SubmissionPlan, current_form: FormVersion) -> SubmissionPlan:
    """执行前核对当前表单；变化时使已确认计划失效。"""
    if not isinstance(current_form, FormVersion):
        raise TypeError("current_form must be a FormVersion")
    reason: dict[str, Any] | None = None
    if current_form.key != plan.form.key:
        reason = {
            "code": "form_version_changed",
            "detail": "表单作用域已变化："
            + plan.form.key
            + " → "
            + current_form.key,
            "planned": plan.form.key,
            "current": current_form.key,
        }
    elif current_form.fingerprint() != plan.form.fingerprint():
        missing = [name for name in plan.form.names() if not current_form.has(name)]
        reason = {
            "code": "form_structure_changed",
            "detail": "表单版本号未变但字段结构已变化，计划不能继续执行。",
            "planned_fingerprint": plan.form.fingerprint(),
            "current_fingerprint": current_form.fingerprint(),
            "missing_fields": missing,
        }
    if reason is None:
        return plan
    return replace(
        plan,
        invalidated_at=_now(),
        invalidation=reason,
    )


def portal_payload(
    plan: SubmissionPlan,
    current_form: FormVersion,
    csrf: str | None = None,
    file_by_id: dict[str, MaterialFile] | None = None,
) -> dict[str, Any]:
    """生成门户提交数据，但绝不发起提交。

    先做与 :func:`preflight` 相同的新鲜度检查；失效计划返回 ``executable``
    为 False，且不产生可提交载荷。``csrf`` 缺省时只做预检，不生成令牌字段。
    """
    checked = preflight(plan, current_form)
    blockers = list(checked.blockers)
    if plan.status != "ready_for_review":
        blockers.append(
            {"code": "plan_not_ready", "detail": "计划尚未通过审阅（可能缺少必填材料）。"}
        )
    if checked.confirm_token is None:
        blockers.append({"code": "plan_unconfirmed", "detail": "计划尚未经申报员确认。"})
    if checked.invalidated_at is not None:
        blockers.append(
            {
                "code": "plan_invalidated",
                "detail": "表单已变化，必须重新生成计划并再次确认。",
                "reason": checked.invalidation,
            }
        )
    covered = {item.field for item in checked.entries}
    for name in current_form.required_names():
        if name not in covered:
            blockers.append(
                {
                    "code": "portal_required_unmet",
                    "field": name,
                    "detail": "门户必填项没有计划材料。",
                }
            )
    for entry in checked.entries:
        if not current_form.has(entry.field):
            blockers.append(
                {
                    "code": "planned_field_missing",
                    "field": entry.field,
                    "detail": "计划引用的字段在当前表单中不存在。",
                }
            )
        if file_by_id is not None and entry.file_id not in file_by_id:
            blockers.append(
                {
                    "code": "planned_file_missing",
                    "file_id": entry.file_id,
                    "detail": "计划引用的原文件不可用。",
                }
            )
    fields: dict[str, str] = {}
    for entry in checked.entries:
        fields[entry.field] = entry.file_id
    return {
        "plan_id": checked.plan_id,
        "application_id": checked.application_id,
        "form": checked.form.key,
        "executable": not blockers,
        "blockers": blockers,
        "fields": fields if not blockers else {},
        "csrf": csrf if (csrf and not blockers) else None,
        "note": "仅生成本地演练载荷，未发送任何提交请求。",
    }
