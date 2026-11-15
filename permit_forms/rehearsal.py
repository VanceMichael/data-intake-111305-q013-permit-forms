"""从合成场景装载映射演练所需的表单、文件与人工记录。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .forms import FormVersion, Scope, parse_form_version
from .ledger import Ledger
from .mapping import CaseDecision, MappingRule, MaterialFile


def scenario_path(path: Path | None = None) -> Path:
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        return candidate
    packaged = Path(__file__).parent / "fixtures" / "rehearsal.json"
    if packaged.is_file():
        return packaged
    source = Path(__file__).parents[1] / "fixtures" / "rehearsal.json"
    if not source.is_file():
        raise FileNotFoundError("rehearsal fixture not found")
    return source


def load_scenario(path: Path | None = None) -> dict[str, Any]:
    return json.loads(scenario_path(path).read_text(encoding="utf-8"))


def forms_from_scenario(data: dict[str, Any]) -> dict[str, FormVersion]:
    return {
        item.key: item
        for item in (parse_form_version(raw) for raw in data.get("forms", ()))
    }


def files_from_scenario(data: dict[str, Any], application_id: str) -> tuple[MaterialFile, ...]:
    for application in data.get("applications", ()):
        if application["application_id"] != application_id:
            continue
        return tuple(_file(raw) for raw in application.get("files", ()))
    raise KeyError(application_id)


def application_from_scenario(data: dict[str, Any], application_id: str) -> dict[str, Any]:
    for application in data.get("applications", ()):
        if application["application_id"] == application_id:
            return application
    raise KeyError(application_id)


def _file(raw: dict[str, Any]) -> MaterialFile:
    return MaterialFile(
        file_id=raw["file_id"],
        name=raw["name"],
        digest=raw["digest"],
        kind=raw["kind"],
        districts=tuple(raw.get("districts", ())),
        application_types=tuple(raw.get("application_types", ())),
    )


def _scope(raw: dict[str, Any]) -> Scope:
    return Scope(raw["district"], raw["application_type"], raw["form_version"])


def rules_from_scenario(data: dict[str, Any]) -> tuple[MappingRule, ...]:
    return tuple(
        MappingRule(
            rule_id=raw["rule_id"],
            scope=_scope(raw["scope"]),
            target_field=raw["target_field"],
            accepts=tuple(raw["accepts"]),
            origin=raw.get("origin", "manual"),
            note=raw.get("note", ""),
            placement=raw.get("placement", "primary"),
        )
        for raw in data.get("rules", ())
    )


def decisions_from_scenario(data: dict[str, Any]) -> tuple[CaseDecision, ...]:
    return tuple(
        CaseDecision(
            decision_id=raw["decision_id"],
            application_id=raw["application_id"],
            file_id=raw["file_id"],
            scope=_scope(raw["scope"]),
            action=raw["action"],
            note=raw["note"],
            target_field=raw.get("target_field"),
            placement=raw.get("placement", "primary"),
        )
        for raw in data.get("case_decisions", ())
    )


def load_ledger(
    data: dict[str, Any], path: Path | None = None, ledger: Ledger | None = None
) -> Ledger:
    """把场景声明的决定补入记录；已存在同编号记录时跳过，可重复加载。"""
    ledger = ledger if ledger is not None else Ledger(path)
    known_rules = {item.rule_id for item in ledger.all_rule_versions()}
    known_decisions = {item.decision_id for item in ledger.case_decisions()}
    for rule in rules_from_scenario(data):
        if rule.rule_id in known_rules:
            continue
        ledger.add_rule(rule, "fixture", "场景声明的可复用规则")
    for decision in decisions_from_scenario(data):
        if decision.decision_id in known_decisions:
            continue
        ledger.add_case_decision(decision, "fixture", "场景声明的项目内例外")
    return ledger
