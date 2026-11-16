"""门户表单快照：字段集合、表单版本与 csrf 令牌绑定为一个确定性摘要。

填报计划在确认时锚定某个快照摘要；执行前重新观察门户，摘要不一致即拒绝，
保证“确认之后表单又变化，计划不得继续执行”。摘要只依赖声明内容，不做任何
相似度推断。
"""

import hashlib
import json
from dataclasses import dataclass

from .decoding import parse_page, records


@dataclass(frozen=True)
class FormSnapshot:
    district: str
    application_type: str
    form_version: str
    csrf: str
    fields: tuple
    digest: str

    @property
    def required_fields(self):
        return [field["field"] for field in self.fields if field["required"]]

    def field_names(self):
        return [field["field"] for field in self.fields]


def build_snapshot(district, application_type, form_version, csrf, fields):
    """规范化字段列表并计算快照摘要。字段顺序不影响摘要。"""
    for label, value in (("district", district), ("application_type", application_type),
                         ("form_version", form_version), ("csrf", csrf)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(label + " must be a non-empty string")
    normalized = []
    seen = set()
    for item in fields:
        name = str(item.get("field", "")).strip()
        if not name:
            raise ValueError("field name must be non-empty")
        if name in seen:
            raise ValueError("duplicate field in form: " + name)
        seen.add(name)
        normalized.append({
            "field": name,
            "label": str(item.get("label") or name),
            "required": bool(item.get("required")),
        })
    canonical = {
        "district": district.strip(),
        "application_type": application_type.strip(),
        "form_version": form_version.strip(),
        "csrf": csrf,
        "fields": sorted(normalized, key=lambda item: item["field"]),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return FormSnapshot(
        district.strip(), application_type.strip(), form_version.strip(),
        csrf, tuple(normalized), hashlib.sha256(encoded).hexdigest(),
    )


def snapshot_from_page(body, content_type, district, application_type):
    """从门户表单页提取指定范围（地区/申报类别）的快照。

    字段来自页内 source-records 数据块，csrf 来自表单隐藏控件；两者缺一
    都直接报错，不用缺省值顶替。
    """
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "text/html":
        raise ValueError("portal form page must be text/html, got: " + (media_type or "unknown"))
    observed = records(body, content_type)
    selected = [
        item for item in observed
        if str(item.get("district", "")).strip() == district
        and str(item.get("application_type", "")).strip() == application_type
    ]
    if not selected:
        raise ValueError(
            "no form fields observed for district=%s application_type=%s" % (district, application_type)
        )
    versions = {str(item.get("form_version", "")).strip() for item in selected}
    versions.discard("")
    if len(versions) != 1:
        raise ValueError("form page mixes versions: " + ", ".join(sorted(versions)))
    csrf = None
    for form_control in parse_page(body).forms:
        if form_control["fields"].get("csrf"):
            csrf = form_control["fields"]["csrf"]
            break
    if not csrf:
        raise ValueError("portal csrf token missing from form page")
    return build_snapshot(district, application_type, versions.pop(), csrf, selected)
