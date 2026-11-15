"""表单版本模型、结构差异，以及从来源观察导出的表单版本。

差异只依据表单本身声明的字段关系（``split_of``、``merged_from``、
``renamed_from``）判断，不比较字段名称是否相似，也不做任何模糊推断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import non_empty as _non_empty, stable_text as _stable_text


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    required: bool = True
    source: str = ""
    accepts: tuple[str, ...] = ()
    split_of: str | None = None
    merged_from: tuple[str, ...] = ()
    renamed_from: str | None = None

    def __post_init__(self):
        _non_empty(self.name, "field.name")
        if self.split_of and self.merged_from:
            raise ValueError("field cannot both split and merge: " + self.name)
        if self.renamed_from and (self.split_of or self.merged_from):
            raise ValueError("renamed field cannot also be split or merged: " + self.name)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "required": self.required,
        }
        if self.source:
            result["source"] = self.source
        if self.accepts:
            result["accepts"] = list(self.accepts)
        if self.split_of:
            result["split_of"] = self.split_of
        if self.merged_from:
            result["merged_from"] = list(self.merged_from)
        if self.renamed_from:
            result["renamed_from"] = self.renamed_from
        return result


@dataclass(frozen=True)
class Scope:
    """一张表单的精确作用域。

    映射、规则和项目内例外都以它为键；地区、申报类别、表单版本三者
    必须同时相等才算同一作用域。
    """

    district: str
    application_type: str
    form_version: str

    def __post_init__(self):
        for name in ("district", "application_type", "form_version"):
            _non_empty(getattr(self, name), name)

    @property
    def key(self) -> str:
        return "|".join((self.district, self.application_type, self.form_version))

    @classmethod
    def of(cls, form: "FormVersion") -> "Scope":
        return cls(form.district, form.application_type, form.version)

    def as_dict(self) -> dict[str, str]:
        return {
            "district": self.district,
            "application_type": self.application_type,
            "form_version": self.form_version,
        }


@dataclass(frozen=True)
class FormVersion:
    district: str
    application_type: str
    version: str
    fields: tuple[FormField, ...]

    def __post_init__(self):
        for name in ("district", "application_type", "version"):
            _non_empty(getattr(self, name), name)
        if len({item.name for item in self.fields}) != len(self.fields):
            raise ValueError("duplicate form field")

    @property
    def key(self) -> str:
        return "|".join((self.district, self.application_type, self.version))

    def fingerprint(self) -> str:
        """表单字段结构的稳定摘要，供计划失效检查使用。"""
        return _stable_text([item.as_dict() for item in self.fields])

    def field(self, name: str) -> FormField:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(item.name == name for item in self.fields)

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields)

    def required_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.required)

    def as_dict(self) -> dict[str, Any]:
        return {
            "district": self.district,
            "application_type": self.application_type,
            "version": self.version,
            "fields": [item.as_dict() for item in self.fields],
        }


def parse_form_version(raw: dict[str, Any]) -> FormVersion:
    if not isinstance(raw, dict):
        raise TypeError("form version must be an object")
    entries = raw.get("fields", ())
    if not isinstance(entries, (list, tuple)):
        raise TypeError("fields must be a list")
    fields = []
    for entry in entries:
        accepts = entry.get("accepts", ())
        if isinstance(accepts, str):
            accepts = (accepts,)
        merged = entry.get("merged_from", ())
        if isinstance(merged, str):
            merged = (merged,)
        fields.append(
            FormField(
                name=str(entry.get("name", "")).strip(),
                label=str(entry.get("label", entry.get("name", ""))),
                required=bool(entry.get("required", True)),
                source=str(entry.get("source", "")),
                accepts=tuple(str(item) for item in accepts),
                split_of=entry.get("split_of"),
                merged_from=tuple(str(item) for item in merged),
                renamed_from=entry.get("renamed_from"),
            )
        )
    return FormVersion(
        district=str(raw.get("district", "")).strip(),
        application_type=str(raw.get("application_type", "")).strip(),
        version=str(raw.get("version", "")).strip(),
        fields=tuple(fields),
    )


@dataclass(frozen=True)
class FieldChange:
    kind: str  # removed / added / split / merged / renamed / retyped / required_changed
    field: str
    related: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "field": self.field}
        if self.related:
            result["related"] = list(self.related)
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class FormDiff:
    from_key: str
    to_key: str
    changes: tuple[FieldChange, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_key,
            "to": self.to_key,
            "changes": [item.as_dict() for item in self.changes],
        }

    def affected_fields(self) -> frozenset[str]:
        result: set[str] = set()
        for change in self.changes:
            result.add(change.field)
            result.update(change.related)
        return frozenset(result)


def diff_forms(before: FormVersion, after: FormVersion) -> FormDiff:
    """比较两个表单版本，只使用表单声明的结构关系。"""
    before_fields = {item.name: item for item in before.fields}
    after_fields = {item.name: item for item in after.fields}
    changes: list[FieldChange] = []

    splits: dict[str, list[str]] = {}
    merges: dict[tuple[str, ...], list[str]] = {}
    renames: dict[str, str] = {}
    for name, item in after_fields.items():
        if item.split_of:
            splits.setdefault(item.split_of, []).append(name)
        if item.merged_from:
            merges.setdefault(item.merged_from, []).append(name)
        if item.renamed_from:
            renames[item.renamed_from] = name

    removed = [name for name in before_fields if name not in after_fields]
    added = [name for name in after_fields if name not in before_fields]

    for name in removed:
        if name in splits:
            changes.append(
                FieldChange("split", name, tuple(sorted(splits[name])), "字段在新版本中拆分为多个")
            )
        elif name in renames:
            changes.append(
                FieldChange("renamed", name, (renames[name],), "字段在新版本中改名")
            )
        elif any(name in group for group in merges):
            # 由 merged 记录在目标字段上报告，避免重复
            continue
        else:
            changes.append(FieldChange("removed", name, (), "字段在新版本中不存在"))

    for name in added:
        item = after_fields[name]
        if item.split_of:
            continue  # 已由 split 记录覆盖
        if item.merged_from:
            changes.append(
                FieldChange("merged", name, item.merged_from, "多个旧字段合并为一个")
            )
            continue
        if name in renames.values():
            continue  # 已由 renamed 记录覆盖
        changes.append(FieldChange("added", name, (), "新版本新增字段"))

    for name in sorted(set(before_fields) & set(after_fields)):
        old, new = before_fields[name], after_fields[name]
        if old.accepts != new.accepts or old.source != new.source:
            changes.append(
                FieldChange("retyped", name, (), "接受的材料类型或来源线索发生变化")
            )
        if old.required != new.required:
            changes.append(
                FieldChange(
                    "required_changed",
                    name,
                    (),
                    "必填状态从 True 变为 False" if old.required else "必填状态从 False 变为 True",
                )
            )

    return FormDiff(before.key, after.key, tuple(changes))


# ---------------------------------------------------------------------------
# 从来源记录构建表单版本快照
# ---------------------------------------------------------------------------


def form_versions_from_records(records: list[dict[str, Any]]) -> dict[str, FormVersion]:
    """把 ``/feed`` 里的字段级记录按作用域聚合为表单版本。

    每条来源记录声明一个字段。同一条记录里附带的结构声明
    （``split_of``、``merged_from``、``renamed_from``、``accepts``）原样
    带入，不做补全或推断。
    """
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise TypeError("source record must be an object")
        district = str(raw.get("district", "")).strip()
        application_type = str(raw.get("application_type", "")).strip()
        version = str(raw.get("form_version", "")).strip()
        for name, value in (
            ("district", district),
            ("application_type", application_type),
            ("form_version", version),
            ("field", str(raw.get("field", "")).strip()),
        ):
            if not value:
                raise ValueError("source record missing " + name)
        grouped.setdefault((district, application_type, version), []).append(raw)

    result: dict[str, FormVersion] = {}
    for (district, application_type, version), items in grouped.items():
        fields = []
        for raw in items:
            accepts = raw.get("accepts", ())
            if isinstance(accepts, str):
                accepts = (accepts,)
            merged = raw.get("merged_from", ())
            if isinstance(merged, str):
                merged = (merged,)
            fields.append(
                FormField(
                    name=str(raw["field"]).strip(),
                    label=str(raw.get("label", raw["field"])),
                    required=bool(raw.get("required", True)),
                    source=str(raw.get("source_hint", "")),
                    accepts=tuple(str(item) for item in accepts),
                    split_of=raw.get("split_of"),
                    merged_from=tuple(str(item) for item in merged),
                    renamed_from=raw.get("renamed_from"),
                )
            )
        form = FormVersion(district, application_type, version, tuple(fields))
        result[form.key] = form
    return result
