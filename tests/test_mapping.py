import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from permit_forms.archive import Archive
from permit_forms.client import Client
from permit_forms.drill import run_drill, scenario_path
from permit_forms.forms import build_snapshot, snapshot_from_page
from permit_forms.mapping import MappingStore
from permit_forms.origin import load_scenario, serve

FIELDS_B = [
    ("industry_a", "行业资质一", True),
    ("industry_b", "行业资质二", True),
    ("remark", "备注", False),
]
FIELDS_C = [
    ("industry_a", "行业资质一", True),
    ("industry_b1", "行业资质二·正本", True),
    ("industry_b2", "行业资质二·副本", True),
    ("remark", "备注", False),
]


def west_form(version="2026-b", csrf="tok-1", fields=None):
    raw = fields if fields is not None else FIELDS_B
    return build_snapshot(
        "west", "lab", version, csrf,
        [{"field": name, "label": label, "required": required} for name, label, required in raw],
    )


class SnapshotTests(unittest.TestCase):
    def test_digest_ignores_field_order(self):
        forward = west_form()
        reversed_fields = list(reversed(FIELDS_B))
        backward = west_form(fields=reversed_fields)
        self.assertEqual(forward.digest, backward.digest)
        self.assertNotEqual(forward.digest, west_form(csrf="tok-2").digest)
        self.assertNotEqual(forward.digest, west_form(version="2026-c").digest)

    def test_snapshot_from_page_reads_fields_and_csrf(self):
        with serve(load_scenario(scenario_path())) as origin:
            response = Client(origin.base_url).fetch("/form/current")
            form = snapshot_from_page(response.body, response.headers["content-type"], "west", "lab")
            self.assertEqual(form.form_version, "2026-b")
            self.assertEqual(form.csrf, "west-form-2026-b")
            self.assertEqual(form.required_fields, ["industry_a", "industry_b"])

    def test_snapshot_rejects_mixed_versions_and_missing_csrf(self):
        mixed = (b'<form><input name="csrf" value="x"></form>'
                 b'<script id="source-records" type="application/json">'
                 b'[{"district":"west","application_type":"lab","form_version":"a","field":"f1"},'
                 b'{"district":"west","application_type":"lab","form_version":"b","field":"f2"}]</script>')
        with self.assertRaisesRegex(ValueError, "mixes versions"):
            snapshot_from_page(mixed, "text/html", "west", "lab")
        no_csrf = (b'<script id="source-records" type="application/json">'
                   b'[{"district":"west","application_type":"lab","form_version":"a","field":"f1"}]</script>')
        with self.assertRaisesRegex(ValueError, "csrf"):
            snapshot_from_page(no_csrf, "text/html", "west", "lab")
        with self.assertRaisesRegex(ValueError, "no form fields"):
            snapshot_from_page(no_csrf.replace(b'"west"', b'"east"'), "text/html", "west", "lab")


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MappingStore(Path(self.temporary.name) / "mapping.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add_doc(self, project="P1", kind="industry_certificate", name="cert.pdf"):
        return self.store.register_document(project, kind, name, "digest-" + project + "-" + name)

    def west_ruling(self, **overrides):
        params = dict(
            scope_type="reusable", district="west", application_type="lab",
            form_version="2026-b", doc_kind="industry_certificate",
            targets=["industry_a", "industry_b"], rationale="乙区拆分两栏", author="agent",
        )
        params.update(overrides)
        return self.store.record_ruling(**params)

    def test_rule_applies_only_to_exact_scope(self):
        self.add_doc()
        self.west_ruling()
        report = self.store.suggest("P1", west_form())
        self.assertTrue(report["complete"])
        other_version = self.store.suggest("P1", west_form(version="2026-c", fields=FIELDS_C))
        self.assertFalse(other_version["complete"])
        self.assertEqual(
            {entry["status"] for entry in other_version["fields"]},
            {"insufficient_evidence"},
        )
        east_form = build_snapshot("east", "lab", "2026-b", "tok", [
            {"field": "business_licence", "label": "经营许可", "required": True},
        ])
        other_district = self.store.suggest("P1", east_form)
        self.assertEqual(other_district["fields"][0]["status"], "insufficient_evidence")

    def test_insufficient_evidence_lists_checks(self):
        report = self.store.suggest("P1", west_form())
        self.assertEqual(report["missing_required"], ["industry_a", "industry_b"])
        entry = report["fields"][0]
        self.assertEqual(entry["status"], "insufficient_evidence")
        kinds = {(item["type"], item["result"]) for item in entry["checked"]}
        self.assertIn(("reusable_rule", "none"), kinds)
        self.assertIn(("project_ruling", "none"), kinds)

    def test_suggestion_explains_basis(self):
        document_id = self.add_doc()
        ruling_id = self.west_ruling()
        report = self.store.suggest("P1", west_form())
        entry = report["fields"][0]
        self.assertEqual(entry["status"], "suggested")
        self.assertEqual(entry["document"]["id"], document_id)
        basis = entry["basis"]
        self.assertEqual(basis["type"], "reusable_rule")
        self.assertEqual(basis["origin_ruling"], ruling_id)
        self.assertEqual(basis["rationale"], "乙区拆分两栏")
        self.assertEqual(basis["scope"]["form_version"], "2026-b")

    def test_refresh_does_not_overwrite_rulings(self):
        self.add_doc()
        self.west_ruling()
        form = west_form()
        first = self.store.suggest("P1", form)
        impact = self.store.register_snapshot(form, capture_id=1)
        self.assertFalse(impact["changed"])
        second = self.store.suggest("P1", form)
        self.assertEqual(
            [(e["field"], e["status"], e.get("basis", {}).get("rule_id")) for e in first["fields"]],
            [(e["field"], e["status"], e.get("basis", {}).get("rule_id")) for e in second["fields"]],
        )

    def test_project_ruling_stays_within_project(self):
        self.add_doc("P1")
        self.add_doc("P2")
        self.add_doc("P2", kind="industry_certificate_supplement", name="sup.pdf")
        self.add_doc("P3")
        self.west_ruling()
        self.store.record_ruling(
            scope_type="project", project_id="P2", district="west", application_type="lab",
            form_version="2026-b", doc_kind="industry_certificate_supplement",
            targets=["industry_b"], rationale="仅此项目", author="agent",
        )
        form = west_form()
        basis_by_project = {}
        for project in ("P1", "P2", "P3"):
            report = self.store.suggest(project, form)
            basis_by_project[project] = [
                entry["basis"]["type"] for entry in report["fields"] if entry["field"] == "industry_b"
            ][0]
        self.assertEqual(basis_by_project, {
            "P1": "reusable_rule", "P2": "project_ruling", "P3": "reusable_rule",
        })

    def test_ruling_scope_validation(self):
        with self.assertRaisesRegex(ValueError, "project_id"):
            self.west_ruling(scope_type="project")
        with self.assertRaisesRegex(ValueError, "不绑定单个项目"):
            self.west_ruling(project_id="P1")
        with self.assertRaisesRegex(ValueError, "scope_type"):
            self.west_ruling(scope_type="global")
        with self.assertRaisesRegex(ValueError, "targets"):
            self.west_ruling(targets=[])
        with self.assertRaisesRegex(ValueError, "rationale"):
            self.west_ruling(rationale=" ")

    def test_new_reusable_ruling_supersedes_and_keeps_history(self):
        self.add_doc()
        self.west_ruling()
        self.west_ruling(targets=["industry_a"], rationale="复核后只需一栏")
        statuses = {rule["id"]: rule["status"] for rule in self.store.rules()}
        self.assertEqual(sorted(statuses.values()), ["active", "superseded"])
        report = self.store.suggest("P1", west_form())
        entry_b = [e for e in report["fields"] if e["field"] == "industry_b"][0]
        self.assertEqual(entry_b["status"], "insufficient_evidence")
        historical = [c for c in entry_b["checked"] if c["type"] == "historical_rule"]
        self.assertEqual(historical[0]["result"], "superseded")

    def test_historical_rule_is_context_not_basis_after_upgrade(self):
        self.add_doc()
        self.west_ruling()
        self.store.register_snapshot(west_form(), capture_id=1)
        self.store.register_snapshot(west_form(version="2026-c", csrf="tok-2", fields=FIELDS_C), capture_id=2)
        report = self.store.suggest("P1", west_form(version="2026-c", csrf="tok-2", fields=FIELDS_C))
        entry_a = [e for e in report["fields"] if e["field"] == "industry_a"][0]
        self.assertEqual(entry_a["status"], "insufficient_evidence")
        historical = [c for c in entry_a["checked"] if c["type"] == "historical_rule"]
        self.assertEqual(historical[0]["result"], "impacted")
        self.assertEqual(historical[0]["form_version"], "2026-b")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MappingStore(self.root / "mapping.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def prepare(self, csrf="west-form-2026-b"):
        self.store.register_document("P1", "industry_certificate", "cert.pdf", "digest-p1")
        self.store.record_ruling(
            scope_type="reusable", district="west", application_type="lab",
            form_version="2026-b", doc_kind="industry_certificate",
            targets=["industry_a", "industry_b"], rationale="乙区拆分", author="agent",
        )
        return west_form(csrf=csrf)

    def test_blocked_plan_cannot_be_confirmed_or_executed(self):
        self.prepare()
        form = west_form(fields=FIELDS_B + [("extra", "附加材料", True)])
        plan = self.store.build_plan("P1", form)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["missing"], ["extra"])
        with self.assertRaisesRegex(ValueError, "必填项未满足"):
            self.store.confirm_plan(plan["id"], author="agent-01")
        result = self.store.execute_plan(plan["id"], form, lambda *args: self.fail("不得提交"))
        self.assertEqual(result["reason"], "plan_not_confirmed")

    def test_confirm_requires_draft_and_author(self):
        form = self.prepare()
        plan = self.store.build_plan("P1", form)
        self.assertEqual(plan["status"], "draft")
        with self.assertRaisesRegex(ValueError, "author"):
            self.store.confirm_plan(plan["id"], author=" ")
        with self.assertRaises(KeyError):
            self.store.confirm_plan(999, author="a")

    def test_confirmed_plan_executes_against_local_portal(self):
        form = self.prepare()
        plan = self.store.build_plan("P1", form)
        self.store.confirm_plan(plan["id"], author="agent-01")
        with serve(load_scenario(scenario_path())) as origin:
            def submit(field, document, confirmed):
                payload = urlencode({
                    "id": field, "csrf": confirmed["csrf"], "document": document["digest"],
                }).encode()
                request = Request(origin.base_url + "/portal/submit", data=payload)
                with urlopen(request, timeout=3) as response:
                    return json.loads(response.read())
            result = self.store.execute_plan(plan["id"], form, submit)
            self.assertTrue(result["executed"])
            self.assertEqual(set(result["receipts"]), {"industry_a", "industry_b"})
            status = json.loads(Client(origin.base_url).fetch("/portal/status?id=industry_a").body)
            self.assertEqual(status["receipt"], "ACK-1")
        self.assertEqual(self.store.plan(plan["id"])["status"], "executed")

    def test_execute_refuses_when_form_changed_after_confirmation(self):
        form = self.prepare()
        plan = self.store.build_plan("P1", form)
        self.store.confirm_plan(plan["id"], author="agent-01")
        changed = west_form(version="2026-c", csrf="west-form-2026-c", fields=FIELDS_C)
        with serve(load_scenario(scenario_path())) as origin:
            def submit(*args):
                self.fail("表单已变化，不得向门户提交")
            result = self.store.execute_plan(plan["id"], changed, submit)
            self.assertEqual(result["reason"], "form_changed")
            self.assertFalse(result["executed"])
            self.assertEqual([r for r in origin.requests if r["method"] == "POST"], [])
        self.assertEqual(self.store.plan(plan["id"])["status"], "stale")

    def test_version_change_marks_rules_and_plans_and_preserves_history(self):
        form = self.prepare()
        self.store.register_snapshot(form, capture_id=1)
        plan = self.store.build_plan("P1", form)
        self.store.confirm_plan(plan["id"], author="agent-01")
        upgraded = west_form(version="2026-c", csrf="west-form-2026-c", fields=FIELDS_C)
        report = self.store.register_snapshot(upgraded, capture_id=2)
        self.assertTrue(report["changed"])
        self.assertEqual(report["removed_fields"], ["industry_b"])
        self.assertEqual(report["added_fields"], ["industry_b1", "industry_b2"])
        self.assertEqual(report["kept_fields"], ["industry_a", "remark"])
        self.assertEqual(len(report["impacted_rules"]), 1)
        self.assertEqual(report["stale_plans"], [
            {"id": plan["id"], "project_id": "P1", "status_was": "confirmed"},
        ])
        all_rules = self.store.rules()
        self.assertEqual({rule["status"] for rule in all_rules}, {"impacted"})
        stale = self.store.plan(plan["id"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["assignments"], plan["assignments"])
        self.assertEqual([event["kind"] for event in self.store.events("form_changed")], ["form_changed"])

    def test_same_scope_second_snapshot_without_change_has_no_impact(self):
        form = self.prepare()
        first = self.store.register_snapshot(form, capture_id=1)
        self.assertTrue(first["initial"])
        again = self.store.register_snapshot(form, capture_id=2)
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.store.events("form_changed")), 0)


class TraceabilityTests(unittest.TestCase):
    def test_document_bytes_and_rulings_remain_queryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            store = MappingStore(Path(temporary) / "mapping.sqlite3")
            try:
                content = b"%PDF-synthetic-certificate"
                capture_id = archive.append("project-document", "doc://P1/cert.pdf", 200, {}, content)
                import hashlib
                digest = hashlib.sha256(content).hexdigest()
                document_id = store.register_document(
                    "P1", "industry_certificate", "cert.pdf", digest, meta={"capture_id": capture_id},
                )
                store.record_ruling(
                    scope_type="reusable", district="west", application_type="lab",
                    form_version="2026-b", doc_kind="industry_certificate",
                    targets=["industry_a"], rationale="测试", author="agent",
                )
                self.assertEqual(archive.body(capture_id), content)
                document = store.documents("P1")[0]
                self.assertEqual(document["id"], document_id)
                self.assertEqual(document["meta"]["capture_id"], capture_id)
                rulings = store.rulings()
                self.assertEqual(len(rulings), 1)
                self.assertEqual(rulings[0]["rationale"], "测试")
                kinds = [event["kind"] for event in store.events()]
                self.assertEqual(kinds, ["document_registered", "ruling_recorded"])
            finally:
                store.close()
                archive.close()


class DrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transcript = run_drill()
        cls.steps = {step["step"]: step["data"] for step in cls.transcript["steps"]}

    def test_all_phases_present(self):
        expected = {
            "observe_form", "initial_suggestions", "west_reusable_ruling",
            "suggestions_after_refresh", "project_exception_scope",
            "plan_confirmed_and_executed", "portal_upgraded", "impact_report",
            "stale_plan_refused", "suggestions_after_upgrade",
            "blocked_plan_not_submitted", "re_ruling_for_new_version",
            "new_plan_executed", "traceability",
        }
        self.assertTrue(expected.issubset(set(self.steps)))

    def test_refresh_keeps_ruling_based_suggestions(self):
        data = self.steps["suggestions_after_refresh"]
        self.assertFalse(data["form_changed"])
        statuses = {entry["field"]: entry["status"] for entry in data["report"]["fields"]}
        self.assertEqual(statuses["industry_a"], "suggested")
        self.assertEqual(statuses["industry_b"], "suggested")

    def test_exception_does_not_generalize(self):
        data = self.steps["project_exception_scope"]
        self.assertEqual(data["P1_industry_b_basis"]["type"], "reusable_rule")
        self.assertEqual(data["P2_industry_b_basis"]["type"], "project_ruling")
        self.assertEqual(data["P3_industry_b_basis"]["type"], "reusable_rule")

    def test_version_change_impact_and_refusal(self):
        impact = self.steps["impact_report"]
        self.assertEqual(impact["removed_fields"], ["industry_b"])
        self.assertEqual(impact["added_fields"], ["industry_b1", "industry_b2"])
        self.assertEqual(len(impact["impacted_rules"]), 1)
        self.assertEqual(len(impact["stale_plans"]), 1)
        refusal = self.steps["stale_plan_refused"]
        self.assertFalse(refusal["refusal"]["executed"])
        self.assertEqual(refusal["portal_posts_during_refusal"], 0)
        self.assertEqual(refusal["stale_token_probe_status"], 409)

    def test_blocked_plan_never_submitted(self):
        data = self.steps["blocked_plan_not_submitted"]
        self.assertEqual(data["status"], "blocked")
        self.assertIn("industry_b1", data["missing"])
        self.assertIsNotNone(data["confirm_error"])

    def test_receipts_and_traceability(self):
        receipts = self.steps["new_plan_executed"]["receipts"]
        self.assertEqual(len({item["receipt"] for item in receipts.values()}), 3)
        trace = self.steps["traceability"]
        self.assertGreaterEqual(len(trace["captures"]), 8)
        statuses = [rule["status"] for rule in trace["rules_all_statuses"]]
        self.assertIn("impacted", statuses)
        self.assertIn("active", statuses)
        self.assertEqual(trace["stale_plan"]["status"], "stale")
        self.assertTrue(trace["stale_plan"]["assignments"])


class BusinessCaseTests(unittest.TestCase):
    def test_business_cases_are_concrete(self):
        cases = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "business_cases.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(cases), 3)
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)))
        for case in cases:
            for key in ("name", "situation", "observable"):
                self.assertTrue(case[key].strip())
                self.assertNotEqual(case[key].strip(), key)


if __name__ == "__main__":
    unittest.main()
