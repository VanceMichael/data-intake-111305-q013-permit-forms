"""本地门户演练：映射审阅、计划确认与版本变化的完整衔接。

演练从空的临时目录启动本地门户（origin.py），按顺序完成：

1. 观察乙区表单并登记项目材料（原始页面与文件进入 Archive，可回查）；
2. 甲区历史裁定在场时，乙区建议仍明确“证据不足”，不越区套用；
3. 乙区可复用裁定后，表单刷新不会覆盖人工确认；
4. 项目内例外只影响对应项目，不推广到其他申请；
5. 填报计划确认后执行，取得门户回执；
6. 门户升级拆分字段并轮换令牌：旧映射与未执行计划一并标记影响，
   已确认计划拒绝继续执行，门户自身也拒绝过时令牌；
7. 新字段在重新裁定前保持证据不足（历史规则仅作背景），重新裁定后
   新计划正常执行；
8. 输出可追溯清单：原始抓取、裁定与状态迁移事件、各状态规则。

全部数据为本地合成，不连接任何真实门户。
"""

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .archive import Archive
from .client import Client
from .forms import snapshot_from_page
from .mapping import MappingStore
from .origin import load_scenario, serve

DISTRICT = "west"
APPLICATION_TYPE = "lab"


def scenario_path():
    path = Path(__file__).parent / "fixtures" / "drill.json"
    if not path.is_file():
        path = Path(__file__).parents[1] / "fixtures" / "drill.json"
    return path


def _short(digest, length=12):
    return str(digest)[:length]


def _trim_report(report):
    fields = []
    for entry in report["fields"]:
        item = {"field": entry["field"], "required": entry["required"], "status": entry["status"]}
        if entry["status"] == "suggested":
            item["document"] = {
                "id": entry["document"]["id"],
                "kind": entry["document"]["kind"],
                "digest": _short(entry["document"]["digest"]),
            }
            item["basis"] = entry["basis"]
        else:
            item["checked"] = entry["checked"]
        fields.append(item)
    return {
        "project_id": report["project_id"],
        "form_version": report["scope"]["form_version"],
        "complete": report["complete"],
        "missing_required": report["missing_required"],
        "fields": fields,
    }


def _basis_of(report, field_name):
    for entry in report["fields"]:
        if entry["field"] == field_name:
            return entry.get("basis", {"status": entry["status"], "checked": entry.get("checked")})
    raise KeyError(field_name)


def _post_count(origin):
    return sum(1 for request in origin.requests if request["method"] == "POST")


def _make_submit(origin):
    def submit(field, document, plan):
        payload = urlencode({
            "id": field,
            "csrf": plan["csrf"],
            "project": plan["project_id"],
            "document": document["digest"],
        }).encode()
        request = Request(origin.base_url + "/portal/submit", data=payload)
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read())
    return submit


def run_drill():
    steps = []

    def record(step, summary, data):
        steps.append({"step": step, "summary": summary, "data": data})

    scenario = load_scenario(scenario_path())
    with tempfile.TemporaryDirectory() as temporary:
        archive = Archive(Path(temporary) / "raw.sqlite3")
        store = MappingStore(Path(temporary) / "mapping.sqlite3")
        try:
            with serve(scenario) as origin:
                client = Client(origin.base_url)

                def fetch_form():
                    response = client.fetch("/form/current")
                    capture_id = archive.append(
                        "portal-form", response.url, response.status,
                        response.headers, response.body,
                    )
                    form = snapshot_from_page(
                        response.body, response.headers["content-type"],
                        DISTRICT, APPLICATION_TYPE,
                    )
                    impact = store.register_snapshot(form, capture_id)
                    return form, capture_id, impact

                def register_document(project, kind, name, content):
                    capture_id = archive.append(
                        "project-document", "doc://%s/%s" % (project, name),
                        200, {}, content,
                    )
                    digest = hashlib.sha256(content).hexdigest()
                    document_id = store.register_document(
                        project, kind, name, digest, meta={"capture_id": capture_id},
                    )
                    return document_id, digest

                # 1. 首次观察乙区表单
                form, capture_id, impact = fetch_form()
                record("observe_form", "首次观察乙区（west）实验类申报表单", {
                    "form_version": form.form_version,
                    "csrf": form.csrf,
                    "digest": _short(form.digest),
                    "fields": list(form.fields),
                    "archive_capture_id": capture_id,
                })

                # 2. 登记三个项目的材料，原件进入 Archive
                documents = {}
                for project, kind, name, content in (
                    ("P1", "industry_certificate", "行业证明-鸿图.pdf", b"%PDF-synthetic-P1-industry-certificate"),
                    ("P2", "industry_certificate", "行业证明-峻岭.pdf", b"%PDF-synthetic-P2-industry-certificate"),
                    ("P2", "industry_certificate_supplement", "补充证明-峻岭.pdf", b"%PDF-synthetic-P2-supplement"),
                    ("P3", "industry_certificate", "行业证明-青川.pdf", b"%PDF-synthetic-P3-industry-certificate"),
                ):
                    document_id, digest = register_document(project, kind, name, content)
                    documents.setdefault(project, []).append(
                        {"document_id": document_id, "kind": kind, "name": name, "digest": _short(digest)}
                    )
                record("register_documents", "登记项目材料，原始字节按摘要归档", documents)

                # 3. 甲区历史裁定（对照组）：同一份行业证明在甲区是单一栏目
                east_ruling = store.record_ruling(
                    scope_type="reusable", district="east", application_type="lab",
                    form_version="2026-a", doc_kind="industry_certificate",
                    targets=["business_licence"],
                    rationale="甲区 2026-a 表单将同一行业证明归为经营许可单一栏目",
                    author="agent-lead",
                )
                record("east_ruling_for_contrast", "甲区历史裁定仅在其范围内生效", {
                    "ruling_id": east_ruling, "scope": "east/lab/2026-a",
                })

                # 4. 乙区尚无规则：明确证据不足，甲区规则不越区适用
                initial = store.suggest("P1", form)
                record("initial_suggestions",
                       "乙区无生效规则，逐字段说明证据不足（不套用甲区规则）",
                       _trim_report(initial))

                # 5. 乙区可复用裁定：同一行业证明拆至两个栏目
                west_ruling = store.record_ruling(
                    scope_type="reusable", district="west", application_type="lab",
                    form_version="2026-b", doc_kind="industry_certificate",
                    targets=["industry_a", "industry_b"],
                    rationale="乙区 2026-b 要求同一行业证明拆至行业资质一/二两栏，同一文件复用",
                    author="agent-lead",
                )
                record("west_reusable_ruling", "记录乙区可复用裁定", {
                    "ruling_id": west_ruling, "scope": "west/lab/2026-b",
                    "targets": ["industry_a", "industry_b"],
                })

                # 6. 表单刷新：建议由裁定重新推导，人工确认不被覆盖
                form, _, refresh_impact = fetch_form()
                refreshed = store.suggest("P1", form)
                record("suggestions_after_refresh",
                       "表单刷新后建议仍基于人工裁定，且每条建议可解释", {
                        "form_changed": refresh_impact["changed"],
                        "report": _trim_report(refreshed),
                    })

                # 7. P2 项目内例外：只影响 P2
                exception = store.record_ruling(
                    scope_type="project", project_id="P2",
                    district="west", application_type="lab", form_version="2026-b",
                    doc_kind="industry_certificate_supplement", targets=["industry_b"],
                    rationale="P2 经主管部门确认以补充证明单独覆盖行业资质二，仅此项目",
                    author="agent-lead",
                )
                record("project_exception_scope",
                       "项目内例外不推广：P1/P3 仍依据可复用规则", {
                        "ruling_id": exception,
                        "P1_industry_b_basis": _basis_of(store.suggest("P1", form), "industry_b"),
                        "P2_industry_b_basis": _basis_of(store.suggest("P2", form), "industry_b"),
                        "P3_industry_b_basis": _basis_of(store.suggest("P3", form), "industry_b"),
                    })

                # 8. P1 填报计划：确认并执行，取得回执
                plan1 = store.build_plan("P1", form)
                store.confirm_plan(plan1["id"], author="agent-01")
                fresh, _, _ = fetch_form()
                posts_before = _post_count(origin)
                result1 = store.execute_plan(plan1["id"], fresh, _make_submit(origin))
                record("plan_confirmed_and_executed",
                       "P1 计划确认后执行，门户返回回执", {
                        "plan_id": plan1["id"],
                        "assignments": plan1["assignments"],
                        "executed": result1["executed"],
                        "receipts": result1["receipts"],
                        "portal_posts": _post_count(origin) - posts_before,
                    })

                # 9. P2 计划确认后暂不执行（随后门户升级）
                plan2 = store.build_plan("P2", form)
                store.confirm_plan(plan2["id"], author="agent-01")
                record("plan_confirmed_pending", "P2 计划已确认、尚未执行", {
                    "plan_id": plan2["id"], "status": store.plan(plan2["id"])["status"],
                })

                # 10. 门户升级：2026-c 拆分 industry_b 并轮换令牌
                upgrade = scenario["upgrade"]
                with origin.state_lock:
                    origin.scenario["routes"]["/form/current"] = copy.deepcopy(upgrade["route"])
                    origin.scenario["portal"]["csrf"] = upgrade["csrf"]
                record("portal_upgraded",
                       "门户发布 2026-c：industry_b 拆分为正本/副本两栏，csrf 令牌轮换", {
                        "new_version": "2026-c", "new_csrf": upgrade["csrf"],
                    })

                # 11. 再次观察：影响报告同时标出旧映射与未执行计划
                form2, capture2, impact2 = fetch_form()
                record("impact_report",
                       "版本变化影响：删除/新增字段、impacted 规则、stale 计划一并列出",
                       impact2)

                # 12. 已确认计划遇表单变化：拒绝执行且无任何提交；门户自身亦拒绝旧令牌
                posts_before = _post_count(origin)
                refusal = store.execute_plan(plan2["id"], form2, _make_submit(origin))
                posts_during_refusal = _post_count(origin) - posts_before
                probe_status = None
                probe = Request(
                    origin.base_url + "/portal/submit",
                    data=urlencode({"id": "industry_b", "csrf": plan2["csrf"]}).encode(),
                )
                try:
                    urlopen(probe, timeout=3)
                except HTTPError as error:
                    probe_status = error.code
                record("stale_plan_refused",
                       "确认后的计划遇表单变化不得继续执行（双重防线）", {
                        "refusal": refusal,
                        "portal_posts_during_refusal": posts_during_refusal,
                        "stale_token_probe_status": probe_status,
                    })

                # 13. 升级后的建议：历史规则仅作背景，新字段保持证据不足
                post_upgrade = store.suggest("P1", form2)
                record("suggestions_after_upgrade",
                       "2026-c 下旧规则已 impacted，明确证据不足、不作相似词猜测",
                       _trim_report(post_upgrade))

                # 14. 必填未满足：计划 blocked，不可确认、不提交
                blocked = store.build_plan("P1", form2)
                confirm_error = None
                try:
                    store.confirm_plan(blocked["id"], author="agent-01")
                except ValueError as error:
                    confirm_error = str(error)
                record("blocked_plan_not_submitted",
                       "必填项未满足：输出计划供审阅，但不确认、不提交", {
                        "plan_id": blocked["id"],
                        "status": blocked["status"],
                        "missing": blocked["missing"],
                        "confirm_error": confirm_error,
                    })

                # 15. 针对新版本重新裁定；P2 的旧例外不跨版本泄漏
                ruling_c = store.record_ruling(
                    scope_type="reusable", district="west", application_type="lab",
                    form_version="2026-c", doc_kind="industry_certificate",
                    targets=["industry_a", "industry_b1", "industry_b2"],
                    rationale="2026-c 将行业资质二拆为正本/副本两栏，同一行业证明复用于三栏",
                    author="agent-lead",
                )
                record("re_ruling_for_new_version",
                       "重新裁定后建议恢复；P2 旧例外仅属 2026-b 范围，不泄漏到新版本", {
                        "ruling_id": ruling_c,
                        "P1_report": _trim_report(store.suggest("P1", form2)),
                        "P2_industry_b1_basis": _basis_of(store.suggest("P2", form2), "industry_b1"),
                    })

                # 16. P2 依据新版本重新生成计划并执行
                plan3 = store.build_plan("P2", form2)
                store.confirm_plan(plan3["id"], author="agent-01")
                fresh2, _, _ = fetch_form()
                result3 = store.execute_plan(plan3["id"], fresh2, _make_submit(origin))
                record("new_plan_executed", "P2 新计划确认后执行，门户返回回执", {
                    "plan_id": plan3["id"],
                    "assignments": plan3["assignments"],
                    "executed": result3["executed"],
                    "receipts": result3["receipts"],
                })

                # 17. 可追溯性：原始抓取、事件日志、各状态规则、stale 计划原样可查
                record("traceability", "原文件与人工处理过程仍可查", {
                    "captures": [{
                        "id": item["id"], "source": item["source"], "url": item["url"],
                        "status": item["status"], "digest": _short(item["digest"]),
                    } for item in archive.captures()],
                    "events": [{"id": event["id"], "kind": event["kind"]}
                               for event in store.events()],
                    "rules_all_statuses": [{
                        "id": rule["id"], "status": rule["status"],
                        "form_version": rule["form_version"], "targets": rule["targets"],
                    } for rule in store.rules()],
                    "stale_plan": store.plan(plan2["id"]),
                })
        finally:
            store.close()
            archive.close()
    return {"operation": "material_mapping_drill", "steps": steps}
