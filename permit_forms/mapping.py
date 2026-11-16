"""材料映射、人工裁定与填报计划的持久化及推导。

设计要点：

- 规则按 (district, application_type, form_version) 精确生效；跨地区、跨类别、
  跨版本一律不自动适用。
- 人工裁定分两种：项目内例外（scope_type=project，仅该项目）与可复用规则
  （scope_type=reusable，同范围所有项目）。项目内例外永远不会被推广。
- 建议每次由裁定与规则实时推导，表单刷新不会覆盖人工确认；每条建议都带
  依据（哪条规则/哪份裁定、理由、裁定人），证据不足时明确列出已检查项。
- 历史规则只作背景展示，不作为建议依据，避免误用过去的特殊处理。
- 填报计划锚定表单快照摘要：必填未满足为 blocked 不可确认；确认后表单
  再变化，执行时摘要不一致即拒绝并标记 stale。
- 裁定、规则状态迁移、计划状态迁移与快照对比全部写入只追加的 events
  日志；原始文件与原始页面由 archive.py 按摘要保存，可互相核对。
"""

import json
import sqlite3
from pathlib import Path

SCOPE_PROJECT = "project"
SCOPE_REUSABLE = "reusable"

RULE_ACTIVE = "active"
RULE_IMPACTED = "impacted"
RULE_SUPERSEDED = "superseded"

PLAN_BLOCKED = "blocked"
PLAN_DRAFT = "draft"
PLAN_CONFIRMED = "confirmed"
PLAN_STALE = "stale"
PLAN_EXECUTED = "executed"

OPEN_PLAN_STATUSES = (PLAN_BLOCKED, PLAN_DRAFT, PLAN_CONFIRMED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS rulings (
    id INTEGER PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('project','reusable')),
    project_id TEXT,
    district TEXT NOT NULL,
    application_type TEXT NOT NULL,
    form_version TEXT NOT NULL,
    doc_kind TEXT NOT NULL,
    targets TEXT NOT NULL,
    rationale TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    district TEXT NOT NULL,
    application_type TEXT NOT NULL,
    form_version TEXT NOT NULL,
    doc_kind TEXT NOT NULL,
    targets TEXT NOT NULL,
    origin_ruling INTEGER REFERENCES rulings(id),
    status TEXT NOT NULL CHECK(status IN ('active','impacted','superseded')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    district TEXT NOT NULL,
    application_type TEXT NOT NULL,
    form_version TEXT NOT NULL,
    digest TEXT NOT NULL,
    csrf TEXT NOT NULL,
    fields TEXT NOT NULL,
    capture_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    district TEXT NOT NULL,
    application_type TEXT NOT NULL,
    form_version TEXT NOT NULL,
    form_digest TEXT NOT NULL,
    csrf TEXT NOT NULL,
    assignments TEXT NOT NULL,
    missing TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('blocked','draft','confirmed','stale','executed')),
    confirmed_by TEXT,
    confirmed_at TEXT,
    receipts TEXT,
    staleness_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _require(**values):
    for label, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(label + " must be a non-empty string")


class MappingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)

    def close(self):
        self.connection.close()

    # ------------------------------------------------------------------
    # 事件日志（只追加）
    # ------------------------------------------------------------------
    def _event(self, kind, payload):
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(kind,payload) VALUES (?,?)",
                (kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        return cursor.lastrowid

    def events(self, kind=None):
        query = "SELECT * FROM events"
        params = ()
        if kind is not None:
            query += " WHERE kind=?"
            params = (kind,)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    # ------------------------------------------------------------------
    # 项目材料登记（原始字节由 Archive 按 digest 保存）
    # ------------------------------------------------------------------
    def register_document(self, project_id, kind, name, digest, meta=None):
        _require(project_id=project_id, kind=kind, name=name, digest=digest)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO documents(project_id,kind,name,digest,meta) VALUES (?,?,?,?,?)",
                (project_id, kind, name, digest,
                 json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)),
            )
            document_id = cursor.lastrowid
        self._event("document_registered", {
            "document_id": document_id, "project_id": project_id,
            "kind": kind, "name": name, "digest": digest,
        })
        return document_id

    def documents(self, project_id=None):
        query = "SELECT * FROM documents"
        params = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [{**dict(row), "meta": json.loads(row["meta"])} for row in rows]

    # ------------------------------------------------------------------
    # 人工裁定与规则
    # ------------------------------------------------------------------
    def record_ruling(self, *, scope_type, district, application_type, form_version,
                      doc_kind, targets, rationale, author, project_id=None):
        """记录一笔人工裁定。可复用裁定会生成/替换同范围同材料类型的生效规则。"""
        if scope_type not in (SCOPE_PROJECT, SCOPE_REUSABLE):
            raise ValueError("scope_type must be 'project' or 'reusable'")
        if scope_type == SCOPE_PROJECT and not (project_id or "").strip():
            raise ValueError("项目内例外必须指明 project_id，不得缺省")
        if scope_type == SCOPE_REUSABLE and project_id is not None:
            raise ValueError("可复用规则不绑定单个项目；如需限定项目请用项目内例外")
        targets = [str(target).strip() for target in targets]
        if not targets or any(not target for target in targets):
            raise ValueError("targets must be a non-empty list of field names")
        if len(set(targets)) != len(targets):
            raise ValueError("targets must not contain duplicates")
        _require(district=district, application_type=application_type, form_version=form_version,
                 doc_kind=doc_kind, rationale=rationale, author=author)
        encoded_targets = json.dumps(targets, ensure_ascii=False)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO rulings(scope_type,project_id,district,application_type,"
                "form_version,doc_kind,targets,rationale,author) VALUES (?,?,?,?,?,?,?,?,?)",
                (scope_type, project_id, district, application_type, form_version,
                 doc_kind, encoded_targets, rationale, author),
            )
            ruling_id = cursor.lastrowid
            rule_id = None
            superseded = []
            if scope_type == SCOPE_REUSABLE:
                superseded = [row["id"] for row in self.connection.execute(
                    "SELECT id FROM rules WHERE district=? AND application_type=?"
                    " AND form_version=? AND doc_kind=? AND status=?",
                    (district, application_type, form_version, doc_kind, RULE_ACTIVE),
                ).fetchall()]
                cursor = self.connection.execute(
                    "INSERT INTO rules(district,application_type,form_version,doc_kind,"
                    "targets,origin_ruling,status,note) VALUES (?,?,?,?,?,?,?,?)",
                    (district, application_type, form_version, doc_kind,
                     encoded_targets, ruling_id, RULE_ACTIVE, rationale),
                )
                rule_id = cursor.lastrowid
                for old_id in superseded:
                    self.connection.execute(
                        "UPDATE rules SET status=?, note=? WHERE id=?",
                        (RULE_SUPERSEDED,
                         "superseded by rule %d (ruling %d)" % (rule_id, ruling_id), old_id),
                    )
        self._event("ruling_recorded", {
            "ruling_id": ruling_id, "scope_type": scope_type, "project_id": project_id,
            "district": district, "application_type": application_type,
            "form_version": form_version, "doc_kind": doc_kind, "targets": targets,
            "rationale": rationale, "author": author,
            "rule_id": rule_id, "superseded_rules": superseded,
        })
        return ruling_id

    def _ruling_dict(self, row):
        return {**dict(row), "targets": json.loads(row["targets"])}

    def rulings(self, project_id=None):
        query = "SELECT * FROM rulings"
        params = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [self._ruling_dict(row) for row in rows]

    def _rule_dict(self, row, with_origin=False):
        rule = {**dict(row), "targets": json.loads(row["targets"])}
        if with_origin and rule["origin_ruling"] is not None:
            origin = self.connection.execute(
                "SELECT rationale,author FROM rulings WHERE id=?", (rule["origin_ruling"],),
            ).fetchone()
            if origin is not None:
                rule["rationale"] = origin["rationale"]
                rule["author"] = origin["author"]
        return rule

    def rules(self, status=None, district=None, application_type=None, form_version=None):
        clauses, params = [], []
        for column, value in (("status", status), ("district", district),
                              ("application_type", application_type), ("form_version", form_version)):
            if value is not None:
                clauses.append(column + "=?")
                params.append(value)
        query = "SELECT * FROM rules"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [self._rule_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 字段建议（每次实时推导，可解释）
    # ------------------------------------------------------------------
    def suggest(self, project_id, form):
        """对指定项目与表单快照逐字段给出建议或明确的证据不足说明。"""
        _require(project_id=project_id)
        by_kind = {}
        for document in self.documents(project_id):
            by_kind.setdefault(document["kind"], []).append(document)
        project_rulings = [self._ruling_dict(row) for row in self.connection.execute(
            "SELECT * FROM rulings WHERE scope_type=? AND project_id=? AND district=?"
            " AND application_type=? AND form_version=? ORDER BY id",
            (SCOPE_PROJECT, project_id, form.district, form.application_type, form.form_version),
        ).fetchall()]
        active_rules = [self._rule_dict(row, with_origin=True) for row in self.connection.execute(
            "SELECT * FROM rules WHERE district=? AND application_type=? AND form_version=?"
            " AND status=? ORDER BY id",
            (form.district, form.application_type, form.form_version, RULE_ACTIVE),
        ).fetchall()]
        historical_rules = [self._rule_dict(row, with_origin=True) for row in self.connection.execute(
            "SELECT * FROM rules WHERE district=? AND application_type=? AND status<>? ORDER BY id",
            (form.district, form.application_type, RULE_ACTIVE),
        ).fetchall()]
        entries = [
            self._suggest_field(field, by_kind, project_rulings, active_rules, historical_rules)
            for field in form.fields
        ]
        missing = [entry["field"] for entry in entries
                   if entry["required"] and entry["status"] != "suggested"]
        return {
            "project_id": project_id,
            "scope": {
                "district": form.district,
                "application_type": form.application_type,
                "form_version": form.form_version,
            },
            "form_digest": form.digest,
            "complete": not missing,
            "missing_required": missing,
            "fields": entries,
        }

    def _suggest_field(self, field, by_kind, project_rulings, active_rules, historical_rules):
        name = field["field"]
        entry = {"field": name, "label": field["label"], "required": field["required"]}

        # 1) 项目内例外优先：只查本项目、本范围，绝不引用其他项目的裁定。
        ruling = None
        for candidate in project_rulings:
            if name in candidate["targets"]:
                ruling = candidate  # 同一字段有多笔例外时，以最新一笔为准
        if ruling is not None:
            available = by_kind.get(ruling["doc_kind"], [])
            if available:
                entry.update({
                    "status": "suggested",
                    "document": available[0],
                    "basis": {
                        "type": "project_ruling",
                        "ruling_id": ruling["id"],
                        "project_id": ruling["project_id"],
                        "doc_kind": ruling["doc_kind"],
                        "rationale": ruling["rationale"],
                        "author": ruling["author"],
                        "scope_note": "项目内例外，仅适用于本项目，不推广",
                    },
                    "alternatives": [doc["id"] for doc in available[1:]],
                })
            else:
                entry.update({
                    "status": "insufficient_evidence",
                    "checked": [{
                        "type": "project_ruling",
                        "result": "document_missing",
                        "ruling_id": ruling["id"],
                        "doc_kind": ruling["doc_kind"],
                        "note": "项目内例外要求材料类型 %s，但本项目未登记该类型材料" % ruling["doc_kind"],
                    }],
                })
            return entry

        # 2) 可复用规则：精确匹配当前 (地区, 类别, 表单版本)。
        matched = [rule for rule in active_rules if name in rule["targets"]]
        for rule in matched:
            available = by_kind.get(rule["doc_kind"], [])
            if available:
                entry.update({
                    "status": "suggested",
                    "document": available[0],
                    "basis": {
                        "type": "reusable_rule",
                        "rule_id": rule["id"],
                        "origin_ruling": rule["origin_ruling"],
                        "doc_kind": rule["doc_kind"],
                        "rationale": rule.get("rationale") or rule["note"],
                        "author": rule.get("author"),
                        "scope": {
                            "district": rule["district"],
                            "application_type": rule["application_type"],
                            "form_version": rule["form_version"],
                        },
                    },
                    "alternatives": [doc["id"] for doc in available[1:]],
                })
                competing = [other["id"] for other in matched if other["id"] != rule["id"]]
                if competing:
                    entry["competing_rules"] = competing
                return entry

        # 3) 证据不足：明确列出检查过什么；历史规则仅作背景，不作依据。
        checked = []
        if matched:
            checked.append({
                "type": "reusable_rule",
                "result": "document_missing",
                "rule_ids": [rule["id"] for rule in matched],
                "required_kinds": sorted({rule["doc_kind"] for rule in matched}),
                "note": "存在生效规则，但本项目未登记对应类型的材料",
            })
        else:
            checked.append({
                "type": "reusable_rule",
                "result": "none",
                "note": "当前范围（地区/申报类别/表单版本）无生效规则",
            })
        checked.append({
            "type": "project_ruling",
            "result": "none",
            "note": "本项目无覆盖该字段的项目内例外",
        })
        for rule in historical_rules:
            if name in rule["targets"]:
                checked.append({
                    "type": "historical_rule",
                    "result": rule["status"],
                    "rule_id": rule["id"],
                    "form_version": rule["form_version"],
                    "note": "历史规则仅作背景，不作为建议依据",
                })
        entry.update({"status": "insufficient_evidence", "checked": checked})
        return entry

    # ------------------------------------------------------------------
    # 填报计划
    # ------------------------------------------------------------------
    def _plan_dict(self, row):
        plan = {**dict(row)}
        plan["assignments"] = json.loads(plan["assignments"])
        plan["missing"] = json.loads(plan["missing"])
        if plan["receipts"] is not None:
            plan["receipts"] = json.loads(plan["receipts"])
        return plan

    def plan(self, plan_id):
        row = self.connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self._plan_dict(row)

    def plans(self, project_id=None):
        query = "SELECT * FROM plans"
        params = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [self._plan_dict(row) for row in rows]

    def build_plan(self, project_id, form):
        """生成填报计划。必填项未满足时为 blocked：输出供审阅，但不可确认、不提交。"""
        report = self.suggest(project_id, form)
        assignments = {entry["field"]: entry["document"]["id"]
                       for entry in report["fields"] if entry["status"] == "suggested"}
        missing = report["missing_required"]
        status = PLAN_BLOCKED if missing else PLAN_DRAFT
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO plans(project_id,district,application_type,form_version,"
                "form_digest,csrf,assignments,missing,status) VALUES (?,?,?,?,?,?,?,?,?)",
                (project_id, form.district, form.application_type, form.form_version,
                 form.digest, form.csrf, json.dumps(assignments),
                 json.dumps(missing, ensure_ascii=False), status),
            )
            plan_id = cursor.lastrowid
        self._event("plan_built", {
            "plan_id": plan_id, "project_id": project_id, "form_version": form.form_version,
            "form_digest": form.digest, "status": status,
            "assignments": assignments, "missing": missing, "suggestions": report,
        })
        return self.plan(plan_id)

    def confirm_plan(self, plan_id, author):
        _require(author=author)
        plan = self.plan(plan_id)
        if plan["status"] == PLAN_BLOCKED:
            raise ValueError("必填项未满足，计划不可确认: " + ", ".join(plan["missing"]))
        if plan["status"] != PLAN_DRAFT:
            raise ValueError("only draft plans can be confirmed (current status: %s)" % plan["status"])
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET status=?, confirmed_by=?,"
                " confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (PLAN_CONFIRMED, author, plan_id),
            )
        self._event("plan_confirmed", {"plan_id": plan_id, "author": author})
        return self.plan(plan_id)

    def execute_plan(self, plan_id, current_form, submit):
        """执行已确认的计划。表单快照与计划锚定摘要不一致时拒绝，不发出任何提交。"""
        plan = self.plan(plan_id)
        if plan["status"] == PLAN_STALE:
            return {"executed": False, "plan_id": plan_id, "reason": "plan_stale",
                    "staleness_reason": plan["staleness_reason"]}
        if plan["status"] != PLAN_CONFIRMED:
            return {"executed": False, "plan_id": plan_id,
                    "reason": "plan_not_confirmed", "status": plan["status"]}
        if current_form.digest != plan["form_digest"]:
            with self.connection:
                self.connection.execute(
                    "UPDATE plans SET status=?, staleness_reason=? WHERE id=?",
                    (PLAN_STALE, "form_changed", plan_id),
                )
            self._event("plan_stale", {
                "plan_id": plan_id, "reason": "form_changed",
                "expected_digest": plan["form_digest"], "observed_digest": current_form.digest,
            })
            return {"executed": False, "plan_id": plan_id, "reason": "form_changed"}
        documents = {doc["id"]: doc for doc in self.documents(plan["project_id"])}
        receipts = {}
        for field, document_id in plan["assignments"].items():
            receipts[field] = submit(field, documents[document_id], plan)
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET status=?, receipts=? WHERE id=?",
                (PLAN_EXECUTED, json.dumps(receipts, ensure_ascii=False, sort_keys=True), plan_id),
            )
        self._event("plan_executed", {
            "plan_id": plan_id, "project_id": plan["project_id"], "receipts": receipts,
        })
        return {"executed": True, "plan_id": plan_id, "receipts": receipts}

    # ------------------------------------------------------------------
    # 表单快照登记与版本变化影响
    # ------------------------------------------------------------------
    def register_snapshot(self, form, capture_id=None):
        """登记一次表单观察。与上次观察不一致时，标记受影响规则与计划并返回影响报告。"""
        previous = self.connection.execute(
            "SELECT * FROM snapshots WHERE district=? AND application_type=?"
            " ORDER BY id DESC LIMIT 1",
            (form.district, form.application_type),
        ).fetchone()
        if previous is not None and previous["digest"] == form.digest:
            return {
                "changed": False, "district": form.district,
                "application_type": form.application_type,
                "form_version": form.form_version, "digest": form.digest,
            }
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO snapshots(district,application_type,form_version,digest,csrf,"
                "fields,capture_id) VALUES (?,?,?,?,?,?,?)",
                (form.district, form.application_type, form.form_version, form.digest,
                 form.csrf, json.dumps(list(form.fields), ensure_ascii=False), capture_id),
            )
            snapshot_id = cursor.lastrowid
        if previous is None:
            self._event("form_snapshot", {
                "snapshot_id": snapshot_id, "initial": True, "district": form.district,
                "application_type": form.application_type, "form_version": form.form_version,
                "digest": form.digest, "capture_id": capture_id,
            })
            return {"changed": False, "initial": True, "snapshot_id": snapshot_id,
                    "form_version": form.form_version, "digest": form.digest}

        previous_fields = {item["field"]: item for item in json.loads(previous["fields"])}
        current_fields = {item["field"]: item for item in form.fields}
        removed = sorted(set(previous_fields) - set(current_fields))
        added = sorted(set(current_fields) - set(previous_fields))
        kept = sorted(set(previous_fields) & set(current_fields))
        added_by_label = {}
        for name in added:
            added_by_label.setdefault(current_fields[name]["label"], []).append(name)
        renames = [
            {"from": name, "to": target, "label": previous_fields[name]["label"]}
            for name in removed
            for target in added_by_label.get(previous_fields[name]["label"], [])
        ]
        impacted_rules = self.rules(
            status=RULE_ACTIVE, district=form.district,
            application_type=form.application_type, form_version=previous["form_version"],
        )
        open_plans = [
            plan for plan in self._plans_where(
                "district=? AND application_type=? AND form_digest=?",
                (form.district, form.application_type, previous["digest"]),
            )
            if plan["status"] in OPEN_PLAN_STATUSES
        ]
        with self.connection:
            for rule in impacted_rules:
                self.connection.execute(
                    "UPDATE rules SET status=?, note=? WHERE id=?",
                    (RULE_IMPACTED,
                     "impacted by form change to %s (snapshot %d)" % (form.form_version, snapshot_id),
                     rule["id"]),
                )
            for plan in open_plans:
                self.connection.execute(
                    "UPDATE plans SET status=?, staleness_reason=? WHERE id=?",
                    (PLAN_STALE, "form_changed", plan["id"]),
                )
        report = {
            "changed": True,
            "snapshot_id": snapshot_id,
            "district": form.district,
            "application_type": form.application_type,
            "previous_version": previous["form_version"],
            "current_version": form.form_version,
            "removed_fields": removed,
            "added_fields": added,
            "kept_fields": kept,
            "possible_renames": renames,
            "impacted_rules": [
                {"id": rule["id"], "doc_kind": rule["doc_kind"], "targets": rule["targets"]}
                for rule in impacted_rules
            ],
            "stale_plans": [
                {"id": plan["id"], "project_id": plan["project_id"], "status_was": plan["status"]}
                for plan in open_plans
            ],
        }
        self._event("form_changed", report)
        return report

    def _plans_where(self, where, params):
        rows = self.connection.execute(
            "SELECT * FROM plans WHERE " + where + " ORDER BY id", params,
        ).fetchall()
        return [self._plan_dict(row) for row in rows]
