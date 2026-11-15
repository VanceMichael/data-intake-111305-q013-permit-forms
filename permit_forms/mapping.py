"""可解释的材料映射。

作用域
    映射只按 ``(地区, 申报类别, 表单版本)`` 精确匹配。地区或版本不同，
    即使字段名、材料类型完全相同，也不会被套用。

两类人工裁定
    :class:`CaseDecision` 是项目内例外，只对一笔申请、一份文件、一个表单
    版本生效；:class:`MappingRule` 是显式创建的可复用规则，才对该作用域
    内所有申请生效。例外不会自动变成规则，提升必须走 ``Ledger.promote_case``。

可解释性
    每条建议都带证据链（命中的规则或字段声明、文件声明的材料类型、目标
    字段存在性）和自然语言说明。无法确定时返回 ``insufficient_evidence``
    并列出缺少的证据、以及被考虑但未采用的候选；不比较名称相似度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .forms import FormField, FormVersion, Scope
from .util import non_empty


PLACEMENTS = ("primary", "copy")
CASE_ACTIONS = ("assign", "reject")
RULE_ORIGINS = ("manual", "declared")
MAPPING_STATUSES = (
    "suggested",
    "insufficient_evidence",
    "excluded_by_case_decision",
    "conflicting",
)


@dataclass(frozen=True)
class MaterialFile:
    """已收到、可归入表单的原始材料。

    只有文件自身声明的类型、地区与申报类别参与匹配；文件名不参与推断。
    """

    file_id: str
    name: str
    digest: str
    kind: str
    districts: tuple[str, ...] = ()
    application_types: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("file_id", "name", "digest", "kind"):
            non_empty(getattr(self, name), name)
        if not str(self.digest).startswith("sha256:"):
            raise ValueError("file digest must use sha256:<hex> form")
        for value in self.districts:
            non_empty(value, "file.districts[]")
        for value in self.application_types:
            non_empty(value, "file.application_types[]")

    def district_ok(self, district: str) -> bool:
        return not self.districts or district in self.districts

    def application_type_ok(self, application_type: str) -> bool:
        return not self.application_types or application_type in self.application_types

    def applies_to(self, scope: Scope) -> bool:
        return self.district_ok(scope.district) and self.application_type_ok(scope.application_type)

    def scope_mismatches(self, scope: Scope) -> list[str]:
        result = []
        if not self.district_ok(scope.district):
            result.append("file_district:" + scope.district)
        if not self.application_type_ok(scope.application_type):
            result.append("file_application_type:" + scope.application_type)
        return result

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file_id": self.file_id,
            "name": self.name,
            "digest": self.digest,
            "kind": self.kind,
        }
        if self.districts:
            result["districts"] = list(self.districts)
        if self.application_types:
            result["application_types"] = list(self.application_types)
        return result


@dataclass(frozen=True)
class Evidence:
    kind: str
    value: str
    source: str

    def __post_init__(self):
        non_empty(self.kind, "evidence.kind")
        non_empty(self.source, "evidence.source")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "source": self.source}


@dataclass(frozen=True)
class RejectedCandidate:
    """被考虑过但未被采用的候选，用于解释“为什么不选另一个”。"""

    kind: str
    identifier: str
    reason: str
    target_field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "id": self.identifier,
            "reason": self.reason,
        }
        if self.target_field:
            result["target_field"] = self.target_field
        return result


@dataclass(frozen=True)
class MappingRule:
    """可复用的显式规则，只对其作用域内的所有申请生效。"""

    rule_id: str
    scope: Scope
    target_field: str
    accepts: tuple[str, ...]
    origin: str
    note: str
    placement: str = "primary"
    active: bool = True
    source_decision_id: str | None = None

    def __post_init__(self):
        non_empty(self.rule_id, "rule.rule_id")
        non_empty(self.target_field, "rule.target_field")
        if not self.accepts:
            raise ValueError("rule.accepts must not be empty")
        for kind in self.accepts:
            non_empty(kind, "rule.accepts[]")
        if self.origin not in RULE_ORIGINS:
            raise ValueError("rule.origin must be one of: " + ", ".join(RULE_ORIGINS))
        if self.origin == "manual":
            non_empty(self.note, "rule.note")
        if self.placement not in PLACEMENTS:
            raise ValueError("rule.placement must be one of: " + ", ".join(PLACEMENTS))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule_id": self.rule_id,
            "scope": self.scope.as_dict(),
            "target_field": self.target_field,
            "accepts": list(self.accepts),
            "origin": self.origin,
            "placement": self.placement,
            "active": self.active,
            "note": self.note,
        }
        if self.source_decision_id:
            result["source_decision_id"] = self.source_decision_id
        return result


@dataclass(frozen=True)
class CaseDecision:
    """项目内例外：只作用于一笔申请，不得推广成规则。"""

    decision_id: str
    application_id: str
    file_id: str
    scope: Scope
    action: str
    note: str
    target_field: str | None = None
    placement: str = "primary"

    def __post_init__(self):
        non_empty(self.decision_id, "decision.decision_id")
        non_empty(self.application_id, "decision.application_id")
        non_empty(self.file_id, "decision.file_id")
        non_empty(self.note, "decision.note")
        if self.action not in CASE_ACTIONS:
            raise ValueError("decision.action must be one of: " + ", ".join(CASE_ACTIONS))
        if self.action == "assign" and not self.target_field:
            raise ValueError("assign decision requires target_field")
        if self.action == "reject" and self.target_field:
            raise ValueError("reject decision must not name target_field")
        if self.placement not in PLACEMENTS:
            raise ValueError("decision.placement must be one of: " + ", ".join(PLACEMENTS))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "decision_id": self.decision_id,
            "application_id": self.application_id,
            "file_id": self.file_id,
            "scope": self.scope.as_dict(),
            "action": self.action,
            "placement": self.placement,
            "note": self.note,
        }
        if self.target_field:
            result["target_field"] = self.target_field
        return result


@dataclass(frozen=True)
class Suggestion:
    field: str
    placement: str
    origin: str  # case_decision / manual_rule / declared
    scope_key: str
    evidence: tuple[Evidence, ...]
    explanation: str
    rule_id: str | None = None
    note: str = ""
    case_application_id: str | None = None
    source_decision_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "field": self.field,
            "placement": self.placement,
            "origin": self.origin,
            "scope": self.scope_key,
            "explanation": self.explanation,
            "evidence": [item.as_dict() for item in self.evidence],
        }
        if self.rule_id:
            result["rule_id"] = self.rule_id
        if self.case_application_id:
            result["case_application_id"] = self.case_application_id
        if self.source_decision_id:
            result["source_decision_id"] = self.source_decision_id
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class FileMapping:
    file: MaterialFile
    status: str
    suggestions: tuple[Suggestion, ...] = ()
    reason: dict[str, Any] | None = None
    case_decision_ids: tuple[str, ...] = ()
    rejected_candidates: tuple[RejectedCandidate, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file": self.file.as_dict(),
            "status": self.status,
            "suggestions": [item.as_dict() for item in self.suggestions],
        }
        if self.reason is not None:
            result["reason"] = dict(self.reason)
        if self.case_decision_ids:
            result["case_decision_ids"] = list(self.case_decision_ids)
        if self.rejected_candidates:
            result["rejected_candidates"] = [item.as_dict() for item in self.rejected_candidates]
        return result


@dataclass(frozen=True)
class FieldCoverage:
    field: str
    label: str
    required: bool
    covered: bool
    file_ids: tuple[str, ...]
    placements: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "required": self.required,
            "covered": self.covered,
            "file_ids": list(self.file_ids),
            "placements": list(self.placements),
        }


@dataclass(frozen=True)
class MappingReview:
    application_id: str
    form: FormVersion
    status: str  # complete / incomplete
    files: tuple[FileMapping, ...]
    coverage: tuple[FieldCoverage, ...]
    missing_required: tuple[str, ...]
    unassigned_files: tuple[str, ...]
    notes: tuple[str, ...]
    not_applied_rules: tuple[dict[str, Any], ...] = ()
    not_applied_decisions: tuple[dict[str, Any], ...] = ()
    rule_conflicts: tuple[dict[str, Any], ...] = ()

    def covered(self) -> bool:
        return not self.missing_required

    def as_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "form": self.form.as_dict(),
            "scope": self.form.key,
            "status": self.status,
            "files": [item.as_dict() for item in self.files],
            "coverage": [item.as_dict() for item in self.coverage],
            "missing_required": list(self.missing_required),
            "unassigned_files": list(self.unassigned_files),
            "notes": list(self.notes),
            "not_applied_rules": [dict(item) for item in self.not_applied_rules],
            "not_applied_decisions": [dict(item) for item in self.not_applied_decisions],
            "rule_conflicts": [dict(item) for item in self.rule_conflicts],
        }


# ---------------------------------------------------------------------------
# 审阅入口
# ---------------------------------------------------------------------------


def review_mapping(
    form: FormVersion,
    files: Iterable[MaterialFile],
    rules: Iterable[MappingRule] = (),
    case_decisions: Iterable[CaseDecision] = (),
    application_id: str | None = None,
) -> MappingReview:
    """为一份申请生成文件到字段的映射审阅。

    ``case_decisions`` 必须带有申请标识；不属于该申请的裁定会被忽略并在
    ``not_applied_decisions`` 中说明，绝不会被顺带套用。
    """
    if not isinstance(form, FormVersion):
        raise TypeError("form must be a FormVersion")
    files = tuple(files)
    rules = tuple(rules)
    case_decisions = tuple(case_decisions)
    if case_decisions and not application_id:
        raise ValueError("application_id is required when case decisions are supplied")
    _validate_files(files)
    _validate_decisions(case_decisions)

    scope = Scope.of(form)
    mappings = tuple(
        _map_file(form, scope, item, rules, case_decisions, application_id) for item in files
    )

    coverage = []
    for item in form.fields:
        file_ids = []
        placements = []
        for mapping in mappings:
            for suggestion in mapping.suggestions:
                if suggestion.field == item.name:
                    file_ids.append(mapping.file.file_id)
                    placements.append(suggestion.placement)
        coverage.append(
            FieldCoverage(
                field=item.name,
                label=item.label,
                required=item.required,
                covered=bool(file_ids),
                file_ids=tuple(file_ids),
                placements=tuple(placements),
            )
        )
    missing = tuple(item.field for item in coverage if item.required and not item.covered)
    unassigned = tuple(
        mapping.file.file_id
        for mapping in mappings
        if mapping.status in ("insufficient_evidence", "conflicting")
    )

    notes = []
    for mapping in mappings:
        if len(mapping.suggestions) > 1:
            notes.append(
                "文件 "
                + mapping.file.file_id
                + " 同时被建议到 "
                + str(len(mapping.suggestions))
                + " 个字段："
                + "、".join(item.field for item in mapping.suggestions)
                + "；主归档为 "
                + next(
                    (item.field for item in mapping.suggestions if item.placement == "primary"),
                    "无（全部为复制）",
                )
                + "。"
            )
    used_decisions = sorted(
        {
            decision_id
            for mapping in mappings
            for decision_id in mapping.case_decision_ids
        }
    )
    if used_decisions:
        notes.append(
            "本申请使用了项目内例外 "
            + "、".join(used_decisions)
            + "；这些裁定只对申请 "
            + str(application_id)
            + " 生效，不会推广到其他申请。"
        )

    return MappingReview(
        application_id=application_id or "",
        form=form,
        status="complete" if not missing and not unassigned else "incomplete",
        files=mappings,
        coverage=tuple(coverage),
        missing_required=missing,
        unassigned_files=unassigned,
        notes=tuple(notes),
        not_applied_rules=tuple(_rules_not_applied(scope, rules, files)),
        not_applied_decisions=tuple(_decisions_not_applied(scope, case_decisions, application_id)),
        rule_conflicts=tuple(
            {
                **item,
                "affects_this_application": any(
                    mapping.file.kind == item["material_kind"]
                    and mapping.status == "conflicting"
                    for mapping in mappings
                ),
            }
            for item in _rule_conflicts(scope, rules)
        ),
    )


def _validate_files(files: tuple[MaterialFile, ...]):
    seen = set()
    for item in files:
        if not isinstance(item, MaterialFile):
            raise TypeError("files must contain MaterialFile")
        if item.file_id in seen:
            raise ValueError("duplicate file: " + item.file_id)
        seen.add(item.file_id)


def _validate_decisions(decisions: tuple[CaseDecision, ...]):
    seen = set()
    by_scope: dict[tuple[str, str, str], list[CaseDecision]] = {}
    for item in decisions:
        if not isinstance(item, CaseDecision):
            raise TypeError("case decisions must contain CaseDecision")
        if item.decision_id in seen:
            raise ValueError("duplicate decision: " + item.decision_id)
        seen.add(item.decision_id)
        by_scope.setdefault((item.application_id, item.file_id, item.scope.key), []).append(item)
    for marker, items in by_scope.items():
        rejects = [item for item in items if item.action == "reject"]
        assigns = [item for item in items if item.action == "assign"]
        if rejects and assigns:
            raise ValueError("conflicting case decisions for " + " / ".join(marker))
        if len(rejects) > 1:
            raise ValueError("multiple reject decisions for " + " / ".join(marker))
        targets = [item.target_field for item in assigns]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate assign target for " + " / ".join(marker))


# ---------------------------------------------------------------------------
# 单文件映射
# ---------------------------------------------------------------------------


def _map_file(form, scope, file, rules, case_decisions, application_id) -> FileMapping:
    decisions = tuple(
        item
        for item in case_decisions
        if item.application_id == application_id
        and item.file_id == file.file_id
        and item.scope.key == scope.key
    )
    reject = next((item for item in decisions if item.action == "reject"), None)
    assigns = tuple(item for item in decisions if item.action == "assign")

    if reject is not None:
        return FileMapping(
            file,
            "excluded_by_case_decision",
            (),
            {
                "code": "case_decision_reject",
                "detail": "人工裁定排除了该文件，未套用任何规则或字段声明。",
                "decision_id": reject.decision_id,
                "application_id": reject.application_id,
            },
            (reject.decision_id,),
        )

    rejected: list[RejectedCandidate] = []
    explicit: dict[str, Suggestion] = {}

    # 1) 可复用规则：只允许精确作用域。
    rule_hits: dict[str, list[MappingRule]] = {}
    for rule in rules:
        if not rule.active or rule.scope.key != scope.key:
            continue
        if file.kind not in rule.accepts:
            continue
        if not file.applies_to(scope):
            rejected.append(
                RejectedCandidate(
                    "rule",
                    rule.rule_id,
                    "文件声明的地区或申报类别与该规则作用域不一致",
                    rule.target_field,
                )
            )
            continue
        if not form.has(rule.target_field):
            rejected.append(
                RejectedCandidate(
                    "rule",
                    rule.rule_id,
                    "规则目标字段在当前表单版本中不存在",
                    rule.target_field,
                )
            )
            continue
        rule_hits.setdefault(rule.target_field, []).append(rule)

    for field_name, hits in rule_hits.items():
        placements = {item.placement for item in hits}
        if len(placements) > 1:
            return FileMapping(
                file,
                "conflicting",
                (),
                {
                    "code": "conflicting_rule_placements",
                    "detail": "同一字段的多条规则对主归档/复制归档的声明不一致。",
                    "field": field_name,
                    "missing": ["single_rule_placement"],
                },
                (),
                tuple(rejected),
            )
        chosen = sorted(hits, key=lambda item: item.rule_id)[0]
        for other in hits:
            if other is not chosen:
                rejected.append(
                    RejectedCandidate(
                        "rule",
                        other.rule_id,
                        "同一字段已有更早编号的规则 " + chosen.rule_id,
                        field_name,
                    )
                )
        explicit[field_name] = _rule_suggestion(form, scope, file, chosen)

    # 2) 项目内例外：逐字段覆盖，不抹掉其他字段的规则与声明。
    for decision in assigns:
        field_name = decision.target_field or ""
        if not form.has(field_name):
            rejected.append(
                RejectedCandidate(
                    "case_decision",
                    decision.decision_id,
                    "人工裁定指向的字段在当前表单版本中不存在，不能沿用",
                    field_name,
                )
            )
            continue
        previous = explicit.get(field_name)
        if previous is not None:
            rejected.append(
                RejectedCandidate(
                    previous.origin if previous.origin != "manual_rule" else "rule",
                    previous.rule_id or previous.source_decision_id or previous.field,
                    "本笔申请的人工裁定 " + decision.decision_id + " 优先",
                    field_name,
                )
            )
        explicit[field_name] = _case_suggestion(form, scope, file, decision)

    # 3) 表单字段声明：只补足尚未被规则或例外覆盖的字段。
    declared = []
    if file.applies_to(scope):
        for item in form.fields:
            if item.name in explicit or file.kind not in item.accepts:
                continue
            declared.append(_declared_suggestion(form, scope, file, item))

    explicit_primaries = [item for item in explicit.values() if item.placement == "primary"]
    if len(explicit_primaries) > 1:
        return FileMapping(
            file,
            "conflicting",
            tuple(sorted(explicit.values(), key=lambda item: item.field)),
            {
                "code": "multiple_primary_targets",
                "detail": "同一文件被多条规则或例外声明为多个字段的主归档，需人工裁定。",
                "candidates": [
                    {
                        "field": item.field,
                        "origin": item.origin,
                        "rule_id": item.rule_id,
                        "decision_id": item.source_decision_id,
                    }
                    for item in explicit_primaries
                ],
                "missing": ["single_primary_target"],
            },
            tuple(item.source_decision_id for item in explicit.values() if item.source_decision_id),
            tuple(rejected),
        )

    suggestions = list(explicit.values())
    if declared:
        # 字段声明不区分主归档/复制归档：已有主归档时全部按复制，
        # 否则按表单顺序取第一个为主归档。
        has_primary = bool(explicit_primaries)
        for index, item in enumerate(declared):
            if not has_primary and index == 0:
                suggestions.append(_replace_placement(item, "primary"))
            else:
                suggestions.append(_replace_placement(item, "copy"))

    if not suggestions:
        return FileMapping(
            file,
            "insufficient_evidence",
            (),
            _insufficient_reason(form, scope, file, rules, rejected),
            (),
            tuple(rejected),
        )

    order = {name: index for index, name in enumerate(form.names())}
    suggestions.sort(key=lambda item: (order.get(item.field, 99), PLACEMENTS.index(item.placement)))
    # 只允许一个主归档；显式声明已在上方检查，这里兜底字段声明的排序结果。
    primaries = [item for item in suggestions if item.placement == "primary"]
    if len(primaries) > 1:
        first = primaries[0]
        suggestions = [
            item if item is first or item.placement != "primary" else _replace_placement(item, "copy")
            for item in suggestions
        ]
    return FileMapping(
        file,
        "suggested",
        tuple(suggestions),
        None,
        tuple(sorted({item.source_decision_id for item in suggestions if item.source_decision_id})),
        tuple(rejected),
    )


def _rule_suggestion(form, scope, file, rule) -> Suggestion:
    evidence = [
        Evidence("rule", rule.rule_id, "可复用规则（来源：" + rule.origin + "）"),
        Evidence("rule_scope", rule.scope.key, "规则作用域与当前表单完全一致"),
        Evidence("declared_material_kind", file.kind, "文件 " + file.file_id + " 声明类型"),
        Evidence("target_field", rule.target_field, "表单 " + form.key + " 存在该字段"),
    ]
    if rule.note:
        evidence.append(Evidence("rule_note", rule.note, "规则说明"))
    if rule.source_decision_id:
        evidence.append(
            Evidence(
                "source_decision",
                rule.source_decision_id,
                "由项目内例外显式提升；原例外仍只对原申请生效",
            )
        )
    return Suggestion(
        field=rule.target_field,
        placement=rule.placement,
        origin="manual_rule",
        scope_key=scope.key,
        evidence=tuple(evidence),
        explanation=(
            "文件 "
            + file.file_id
            + " 声明材料类型 "
            + file.kind
            + "，命中规则 "
            + rule.rule_id
            + "（作用域 "
            + rule.scope.key
            + "），建议填入字段 "
            + rule.target_field
            + "。"
        ),
        rule_id=rule.rule_id,
        note=rule.note,
    )


def _case_suggestion(form, scope, file, decision) -> Suggestion:
    evidence = [
        Evidence(
            "case_decision",
            decision.decision_id,
            "人工裁定；仅适用于申请 " + decision.application_id,
        ),
        Evidence("case_scope", decision.application_id, "项目内例外作用域"),
        Evidence("declared_material_kind", file.kind, "文件 " + file.file_id + " 声明类型"),
        Evidence("target_field", decision.target_field or "", "表单 " + form.key + " 存在该字段"),
    ]
    field = form.field(decision.target_field or "")
    if file.kind not in field.accepts:
        evidence.append(
            Evidence(
                "case_override",
                decision.decision_id,
                "字段声明未接受该材料类型；由本笔申请的人工裁定显式覆盖",
            )
        )
    if not file.applies_to(scope):
        evidence.append(
            Evidence(
                "declaration_conflict",
                form.district + "/" + form.application_type,
                "文件声明的地区或申报类别与当前作用域不一致；人工裁定已显式覆盖",
            )
        )
    evidence.append(Evidence("decision_note", decision.note, "裁定理由"))
    return Suggestion(
        field=decision.target_field or "",
        placement=decision.placement,
        origin="case_decision",
        scope_key=scope.key,
        evidence=tuple(evidence),
        explanation=(
            "文件 "
            + file.file_id
            + " 由人工裁定 "
            + decision.decision_id
            + " 指定到字段 "
            + str(decision.target_field)
            + "；该裁定只对申请 "
            + decision.application_id
            + " 生效。"
        ),
        note=decision.note,
        case_application_id=decision.application_id,
        source_decision_id=decision.decision_id,
    )


def _declared_suggestion(form, scope, file, item: FormField) -> Suggestion:
    evidence = [
        Evidence(
            "form_declaration",
            form.key + "/" + item.name,
            "表单字段 accepts 包含该材料类型",
        ),
        Evidence("declared_material_kind", file.kind, "文件 " + file.file_id + " 声明类型"),
        Evidence("target_field", item.name, "当前表单存在该字段"),
    ]
    return Suggestion(
        field=item.name,
        placement="copy",
        origin="declared",
        scope_key=scope.key,
        evidence=tuple(evidence),
        explanation=(
            "文件 "
            + file.file_id
            + " 声明材料类型 "
            + file.kind
            + "，表单 "
            + form.key
            + " 的字段 "
            + item.name
            + " 声明接受该类型。"
        ),
    )


def _replace_placement(item: Suggestion, placement: str) -> Suggestion:
    return Suggestion(
        field=item.field,
        placement=placement,
        origin=item.origin,
        scope_key=item.scope_key,
        evidence=item.evidence,
        explanation=item.explanation,
        rule_id=item.rule_id,
        note=item.note,
        case_application_id=item.case_application_id,
        source_decision_id=item.source_decision_id,
    )


def _insufficient_reason(form, scope, file, rules, rejected) -> dict[str, Any]:
    missing_decision_target = [
        item
        for item in rejected
        if item.kind == "case_decision" and item.reason.startswith("人工裁定指向的字段")
    ]
    if missing_decision_target:
        return {
            "code": "case_decision_target_missing",
            "detail": "人工裁定指向的字段在当前表单版本中不存在，不能沿用。",
            "missing": [
                "field:" + str(item.target_field)
                for item in missing_decision_target
            ],
            "rejected_candidates": [item.as_dict() for item in rejected],
        }
    if not file.applies_to(scope):
        details = []
        if not file.district_ok(scope.district):
            details.append("文件声明地区不包含 " + scope.district)
        if not file.application_type_ok(scope.application_type):
            details.append("文件声明申报类别不包含 " + scope.application_type)
        return {
            "code": "file_scope_mismatch",
            "detail": "；".join(details) + "，不能作为当前作用域的证据。",
            "missing": file.scope_mismatches(scope),
            "rejected_candidates": [item.as_dict() for item in rejected],
        }

    relevant = [item for item in rejected if item.kind == "rule"]
    if relevant:
        return {
            "code": "rule_not_usable",
            "detail": "有规则接受该材料类型，但无法用于当前表单。",
            "missing": ["usable_rule:" + scope.key],
            "rejected_candidates": [item.as_dict() for item in rejected],
        }

    other_versions = [
        item
        for item in _rules_not_applied(scope, rules, (file,))
    ]
    if other_versions:
        return {
            "code": "no_rule_for_current_version",
            "detail": "只有其他作用域的规则接受该材料类型，历史规则未套用到当前版本。",
            "missing": ["rule:" + scope.key + ":" + file.kind],
            "rules_not_applied": other_versions,
        }
    return {
        "code": "no_accepting_rule_or_declaration",
        "detail": "当前作用域下没有规则或字段声明接受该材料类型，证据不足。",
        "missing": ["rule:" + scope.key + ":" + file.kind, "field_accepts:" + file.kind],
    }


# ---------------------------------------------------------------------------
# 未套用项：明确说明历史决定为何没有生效
# ---------------------------------------------------------------------------


def _rules_not_applied(scope, rules, files) -> list[dict[str, Any]]:
    kinds = {item.kind for item in files}
    result = []
    seen = set()
    for rule in rules:
        if not rule.active or rule.scope.key == scope.key or rule.rule_id in seen:
            continue
        if kinds and not (kinds & set(rule.accepts)):
            continue
        reasons = []
        if rule.scope.district != scope.district:
            reasons.append("地区不同")
        if rule.scope.application_type != scope.application_type:
            reasons.append("申报类别不同")
        if not reasons:
            reasons.append("表单版本不同")
        seen.add(rule.rule_id)
        result.append(
            {
                "rule_id": rule.rule_id,
                "scope": rule.scope.key,
                "reason": "；".join(reasons) + "，未套用",
            }
        )
    return sorted(result, key=lambda item: item["rule_id"])


def _decisions_not_applied(scope, decisions, application_id) -> list[dict[str, Any]]:
    result = []
    for item in decisions:
        if item.application_id == application_id and item.scope.key == scope.key:
            continue
        if item.scope.key == scope.key and item.application_id != application_id:
            reason = "属于其他申请的项目内例外，未套用"
        elif item.scope.key != scope.key:
            reason = "作用域不同，未套用"
        else:
            continue
        result.append(
            {
                "decision_id": item.decision_id,
                "application_id": item.application_id,
                "scope": item.scope.key,
                "reason": reason,
            }
        )
    return sorted(result, key=lambda item: item["decision_id"])


def explain_missing(review: MappingReview) -> list[str]:
    """把“证据不足”和冲突整理成申报员可直接阅读的句子。"""
    lines = []
    for mapping in review.files:
        if mapping.status == "suggested":
            continue
        if mapping.status == "excluded_by_case_decision":
            lines.append(
                "文件 "
                + mapping.file.file_id
                + " 被项目内例外排除（"
                + "、".join(mapping.case_decision_ids)
                + "）。"
            )
            continue
        reason = mapping.reason or {}
        lines.append(
            "文件 "
            + mapping.file.file_id
            + " 未定去处（"
            + str(reason.get("code", mapping.status))
            + "）："
            + str(reason.get("detail", ""))
        )
        for candidate in reason.get("rejected_candidates", ()):
            lines.append(
                "  候选 "
                + str(candidate.get("id"))
                + " 未采用："
                + str(candidate.get("reason"))
            )
    return lines


# ---------------------------------------------------------------------------
# 与上次人工确认的差异
# ---------------------------------------------------------------------------


def review_entries(review: MappingReview) -> list[dict[str, Any]]:
    """把审阅结果压成“字段 → 文件”清单，供确认快照和刷新比对使用。"""
    entries = []
    for mapping in review.files:
        for suggestion in mapping.suggestions:
            entries.append(
                {
                    "field": suggestion.field,
                    "file_id": mapping.file.file_id,
                    "placement": suggestion.placement,
                    "origin": suggestion.origin,
                    "rule_id": suggestion.rule_id,
                }
            )
    return sorted(
        entries, key=lambda item: (item["field"], item["file_id"], item["placement"])
    )


def drift_since(review: MappingReview, acknowledgement) -> dict[str, Any]:
    """比较当前建议与申报员上次确认的快照。

    返回新增、移除、去向变化的条目；有变化时 ``requires_review`` 为真，
    调用方应让申报员重新确认，而不是把旧确认沿用下去。
    """
    if acknowledgement is None:
        return {
            "ack_id": None,
            "requires_review": True,
            "detail": "尚无人工确认记录，需申报员先审阅。",
            "added": [],
            "removed": [],
            "changed": [],
        }
    current = review_entries(review)
    previous = [dict(item) for item in acknowledgement.entries]
    key = lambda item: (item["field"], item["file_id"], item["placement"])
    current_map = {key(item): item for item in current}
    previous_map = {key(item): item for item in previous}
    added = [current_map[item] for item in sorted(current_map.keys() - previous_map.keys())]
    removed = [previous_map[item] for item in sorted(previous_map.keys() - current_map.keys())]
    changed = []
    for marker in sorted(current_map.keys() & previous_map.keys()):
        now, before = current_map[marker], previous_map[marker]
        fields = {
            name: {"was": before.get(name), "now": now.get(name)}
            for name in ("origin", "rule_id")
            if before.get(name) != now.get(name)
        }
        if fields:
            changed.append({"field": now["field"], "file_id": now["file_id"], "changes": fields})
    same_form = review.form.fingerprint() == acknowledgement.form_fingerprint
    requires = bool(added or removed or changed) or not same_form
    detail = "建议与上次确认一致。"
    if not same_form:
        detail = "表单结构已变化，上次确认不能沿用。"
    elif added or removed or changed:
        detail = "刷新后的建议与上次确认不同，需要重新确认。"
    return {
        "ack_id": acknowledgement.ack_id,
        "requires_review": requires,
        "detail": detail,
        "same_form": same_form,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _rule_conflicts(scope, rules) -> list[dict[str, Any]]:
    """列出同作用域内“同一材料类型指向不同字段”的规则组合。

    这类规则会让同一文件产生多个主归档，审阅时先标出来；具体到某个文件
    仍会单独记为 ``conflicting``。
    """
    by_kind: dict[str, list[MappingRule]] = {}
    for rule in rules:
        if not rule.active or rule.scope.key != scope.key:
            continue
        for kind in rule.accepts:
            by_kind.setdefault(kind, []).append(rule)
    result = []
    for kind, items in sorted(by_kind.items()):
        targets = sorted({item.target_field for item in items})
        if len(targets) < 2:
            continue
        primaries = sorted(
            {item.target_field for item in items if item.placement == "primary"}
        )
        result.append(
            {
                "material_kind": kind,
                "target_fields": targets,
                "primary_target_fields": primaries,
                "rule_ids": sorted(item.rule_id for item in items),
                "detail": "同一材料类型被多条规则指向不同字段，需人工确定主归档。",
            }
        )
    return result
