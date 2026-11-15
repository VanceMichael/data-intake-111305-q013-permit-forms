"""人工裁定的追加式记录。

* 记录只追加，不覆盖；修正通过新记录并显式指明取代关系完成。
* 项目内例外（case）与可复用规则（rule）是两种不同的记录，例外不会
  自动变成规则；提升操作会创建一条新规则，同时保留原例外的完整历史。
* 每条事件都带序号、时间和原因，可用 :meth:`Ledger.history` 回查。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .forms import Scope
from .mapping import CaseDecision, MappingRule
from .util import digest_text, non_empty, stable_text


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Event:
    sequence: int
    at: str
    kind: str
    subject_id: str
    actor: str
    reason: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "at": self.at,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "actor": self.actor,
            "reason": self.reason,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Acknowledgement:
    """申报员对某次映射审阅结果的确认快照。

    刷新来源后如产生不同建议，快照不会被自动改写；:func:`permit_forms.mapping.drift_since`
    会指出差异，让流程回到人工确认，而不是静默覆盖。
    """

    ack_id: str
    application_id: str
    form_key: str
    form_fingerprint: str
    digest: str
    entries: tuple[dict[str, Any], ...]
    actor: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ack_id": self.ack_id,
            "application_id": self.application_id,
            "form_key": self.form_key,
            "form_fingerprint": self.form_fingerprint,
            "digest": self.digest,
            "entries": [dict(item) for item in self.entries],
            "actor": self.actor,
            "at": self.at,
        }


class Ledger:
    """规则与项目内例外的追加式记录，可选用 SQLite 持久化。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[Event] = []
        self._rules: dict[str, MappingRule] = {}
        self._cases: dict[str, CaseDecision] = {}
        self._acknowledgements: dict[str, Acknowledgement] = {}
        self._superseded_rules: set[str] = set()
        self.connection = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
            self._load()

    # -- 生命周期 ---------------------------------------------------------

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _load(self):
        rows = self.connection.execute("SELECT * FROM decision_events ORDER BY sequence").fetchall()
        for row in rows:
            event = Event(
                row["sequence"],
                row["at"],
                row["kind"],
                row["subject_id"],
                row["actor"],
                row["reason"],
                json.loads(row["payload"]),
            )
            self._apply(event)

    # -- 追加 -------------------------------------------------------------

    def _append(self, kind, subject_id, actor, reason, payload) -> Event:
        non_empty(actor, "actor")
        non_empty(reason, "reason")
        encoded = json.loads(stable_text(payload))
        at = _now()
        if self.connection is not None:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO decision_events(at,kind,subject_id,actor,reason,payload) VALUES (?,?,?,?,?,?)",
                    (at, kind, subject_id, actor, reason, stable_text(encoded)),
                )
            sequence = cursor.lastrowid
        else:
            sequence = len(self._events) + 1
        event = Event(sequence, at, kind, subject_id, actor, reason, encoded)
        self._apply(event)
        return event

    def _apply(self, event: Event):
        if any(item.sequence == event.sequence for item in self._events):
            return
        self._events.append(event)
        payload = event.payload
        if event.kind == "rule_created":
            rule = _rule_from(payload["rule"])
            self._rules[rule.rule_id] = rule
            for replaced in payload.get("supersedes", ()):
                self._superseded_rules.add(str(replaced))
        elif event.kind == "case_decided":
            decision = _decision_from(payload["decision"])
            self._cases[decision.decision_id] = decision
        elif event.kind == "rule_promoted":
            rule = _rule_from(payload["rule"])
            self._rules[rule.rule_id] = rule
        elif event.kind == "rule_deactivated":
            rule = _rule_from(payload["rule"])
            self._rules[rule.rule_id] = rule
        elif event.kind == "review_acknowledged":
            record = payload["acknowledgement"]
            acknowledgement = Acknowledgement(
                ack_id=record["ack_id"],
                application_id=record["application_id"],
                form_key=record["form_key"],
                form_fingerprint=record["form_fingerprint"],
                digest=record["digest"],
                entries=tuple(dict(item) for item in record["entries"]),
                actor=record["actor"],
                at=record["at"],
            )
            self._acknowledgements[acknowledgement.ack_id] = acknowledgement

    # -- 公开操作 ---------------------------------------------------------

    def add_rule(self, rule: MappingRule, actor: str, reason: str, supersedes: Iterable[str] = ()) -> Event:
        if rule.rule_id in self._rules:
            raise ValueError("rule already exists: " + rule.rule_id)
        supersedes = tuple(str(item) for item in supersedes)
        for old in supersedes:
            if old not in self._rules:
                raise ValueError("cannot supersede unknown rule: " + old)
        return self._append(
            "rule_created",
            rule.rule_id,
            actor,
            reason,
            {"rule": rule.as_dict(), "supersedes": list(supersedes)},
        )

    def add_case_decision(self, decision: CaseDecision, actor: str, reason: str) -> Event:
        if decision.decision_id in self._cases:
            raise ValueError("case decision already exists: " + decision.decision_id)
        for existing in self._cases.values():
            if (
                existing.application_id == decision.application_id
                and existing.file_id == decision.file_id
                and existing.scope.key == decision.scope.key
                and existing.decision_id != decision.decision_id
            ):
                raise ValueError(
                    "case decision already covers this application/file/scope: "
                    + existing.decision_id
                    + "; use a correction record instead of a second decision"
                )
        return self._append(
            "case_decided",
            decision.decision_id,
            actor,
            reason,
            {"decision": decision.as_dict()},
        )

    def acknowledge_review(
        self,
        application_id: str,
        form,
        review,
        actor: str,
        reason: str = "申报员已核对本次映射建议",
    ) -> Event:
        """记录一次人工确认，使后续刷新可以对比而不是直接覆盖。"""
        non_empty(application_id, "application_id")
        non_empty(actor, "actor")
        entries = _review_entries(review)
        digest = digest_text(
            {
                "application_id": application_id,
                "form_key": form.key,
                "form_fingerprint": form.fingerprint(),
                "entries": entries,
            }
        )
        ack_id = "ack-" + digest[:16]
        if ack_id in self._acknowledgements:
            raise ValueError("review already acknowledged: " + ack_id)
        at = _now()
        record = Acknowledgement(
            ack_id=ack_id,
            application_id=application_id,
            form_key=form.key,
            form_fingerprint=form.fingerprint(),
            digest=digest,
            entries=entries,
            actor=actor,
            at=at,
        )
        return self._append(
            "review_acknowledged",
            ack_id,
            actor,
            reason,
            {"acknowledgement": record.as_dict()},
        )

    def latest_acknowledgement(self, application_id: str) -> Acknowledgement | None:
        matches = [
            item
            for item in self._acknowledgements.values()
            if item.application_id == application_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.at, item.ack_id))

    def acknowledgements(self, application_id: str | None = None) -> tuple[Acknowledgement, ...]:
        result = list(self._acknowledgements.values())
        if application_id is not None:
            result = [item for item in result if item.application_id == application_id]
        return tuple(sorted(result, key=lambda item: (item.at, item.ack_id)))

    def promote_case(
        self,
        decision_id: str,
        rule_id: str,
        actor: str,
        reason: str,
        accepts: Iterable[str],
        target_field: str | None = None,
        placement: str | None = None,
    ) -> Event:
        """把一条项目内例外显式提升为可复用规则。

        原例外保持不变，仍只对原申请生效；新规则带来源裁定编号，便于追溯。
        ``accepts`` 必须显式给出可复用依据（材料类型），不能从单笔特例
        推断，也不能直接把该申请的文件类型当作通用标准。
        """
        decision = self._cases.get(decision_id)
        if decision is None:
            raise ValueError("unknown case decision: " + decision_id)
        if decision.action != "assign":
            raise ValueError("only assign decisions can be promoted: " + decision_id)
        if rule_id in self._rules:
            raise ValueError("rule already exists: " + rule_id)
        accepts = tuple(str(item).strip() for item in accepts if str(item).strip())
        if not accepts:
            raise ValueError("promotion requires explicit accepts material kinds")
        if decision.file_id not in {item.file_id for item in self._cases.values() if item.decision_id == decision_id}:
            raise ValueError("decision file is not recorded: " + decision.file_id)
        rule = MappingRule(
            rule_id=rule_id,
            scope=decision.scope,
            target_field=target_field or decision.target_field or "",
            accepts=accepts,
            origin="manual",
            note="由项目内例外 " + decision_id + " 显式提升：" + reason,
            placement=placement or decision.placement,
            source_decision_id=decision_id,
        )
        return self._append(
            "rule_promoted",
            rule_id,
            actor,
            reason,
            {"rule": rule.as_dict(), "source_decision_id": decision_id},
        )

    def deactivate_rule(self, rule_id: str, actor: str, reason: str) -> Event:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise ValueError("unknown rule: " + rule_id)
        if not rule.active:
            raise ValueError("rule already inactive: " + rule_id)
        replacement = MappingRule(
            rule_id=rule.rule_id,
            scope=rule.scope,
            target_field=rule.target_field,
            accepts=rule.accepts,
            origin=rule.origin,
            note=rule.note,
            placement=rule.placement,
            active=False,
            source_decision_id=rule.source_decision_id,
        )
        return self._append(
            "rule_deactivated",
            rule_id,
            actor,
            reason,
            {"rule": replacement.as_dict()},
        )

    # -- 查询 -------------------------------------------------------------

    def rules(self, active_only: bool = True) -> tuple[MappingRule, ...]:
        result = []
        for rule in self._rules.values():
            if active_only and (not rule.active or rule.rule_id in self._superseded_rules):
                continue
            result.append(rule)
        return tuple(sorted(result, key=lambda item: item.rule_id))

    def all_rule_versions(self) -> tuple[MappingRule, ...]:
        return tuple(sorted(self._rules.values(), key=lambda item: item.rule_id))

    def case_decisions(self, application_id: str | None = None) -> tuple[CaseDecision, ...]:
        result = list(self._cases.values())
        if application_id is not None:
            result = [item for item in result if item.application_id == application_id]
        return tuple(sorted(result, key=lambda item: item.decision_id))

    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def history(self, subject_id: str) -> tuple[Event, ...]:
        return tuple(item for item in self._events if item.subject_id == subject_id)

    def explain(self, subject_id: str) -> dict[str, Any]:
        events = self.history(subject_id)
        if not events:
            raise KeyError(subject_id)
        latest = events[-1]
        return {
            "subject_id": subject_id,
            "kind": latest.kind,
            "revisions": [item.as_dict() for item in events],
            "current": latest.payload,
        }

    def export(self) -> dict[str, Any]:
        return {"events": [item.as_dict() for item in self._events]}


def _material_kind_for(decision: CaseDecision) -> str:
    """不再从备注文本推断材料类型；保留占位以免旧调用静默出错。"""
    raise ValueError(
        "material kind cannot be inferred from a case decision; pass accepts explicitly"
    )


def _rule_from(payload: dict[str, Any]) -> MappingRule:
    scope = payload["scope"]
    return MappingRule(
        rule_id=payload["rule_id"],
        scope=Scope(scope["district"], scope["application_type"], scope["form_version"]),
        target_field=payload["target_field"],
        accepts=tuple(payload["accepts"]),
        origin=payload["origin"],
        note=payload["note"],
        placement=payload.get("placement", "primary"),
        active=bool(payload.get("active", True)),
        source_decision_id=payload.get("source_decision_id"),
    )


def _decision_from(payload: dict[str, Any]) -> CaseDecision:
    scope = payload["scope"]
    return CaseDecision(
        decision_id=payload["decision_id"],
        application_id=payload["application_id"],
        file_id=payload["file_id"],
        scope=Scope(scope["district"], scope["application_type"], scope["form_version"]),
        action=payload["action"],
        note=payload["note"],
        target_field=payload.get("target_field"),
        placement=payload.get("placement", "primary"),
    )


def _review_entries(review) -> tuple[dict[str, Any], ...]:
    """把审阅结果压成可直接比较的“字段 → 文件”清单。"""
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
    return tuple(sorted(entries, key=lambda item: (item["field"], item["file_id"], item["placement"])))
