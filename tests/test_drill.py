import json
import tempfile
import unittest
from pathlib import Path

from permit_forms.drill import Rehearsal, preflight_report
from permit_forms.ledger import Ledger
from permit_forms.origin import load_scenario as load_origin_scenario, serve
from permit_forms.plan import confirm_plan, portal_payload


class RehearsalReviewTests(unittest.TestCase):
    def setUp(self):
        self.rehearsal = Rehearsal.load()

    def test_two_districts_produce_different_mappings_for_same_material_class(self):
        east = self.rehearsal.review("APP-EAST-001")
        west = self.rehearsal.review("APP-WEST-001")
        self.assertEqual(east["scope"], "east|lab|2026-a")
        self.assertEqual(west["scope"], "west|lab|2026-b")
        # 同一材料类型 industry_cert：甲区一个字段，乙区两个字段
        self.assertEqual(east["files"][0]["file"]["kind"], "industry_cert")
        self.assertEqual(west["files"][0]["file"]["kind"], "industry_cert")
        east_targets = {item["field"] for item in east["files"][0]["suggestions"]}
        west_targets = {item["field"] for item in west["files"][0]["suggestions"]}
        self.assertEqual(east_targets, {"business_licence"})
        self.assertEqual(west_targets, {"industry_a", "industry_b"})
        # 两边的建议来源不同，且作用域互不串用
        east_origins = {item["origin"] for item in east["files"][0]["suggestions"]}
        west_origins = {item["origin"] for item in west["files"][0]["suggestions"]}
        self.assertEqual(east_origins, {"declared"})
        self.assertIn("manual_rule", west_origins)

    def test_rule_is_explainable(self):
        review = self.rehearsal.review("APP-WEST-001")
        by_field = {item["field"]: item for item in review["files"][0]["suggestions"]}
        rule_suggestion = by_field["industry_a"]
        self.assertEqual(rule_suggestion["origin"], "manual_rule")
        self.assertEqual(rule_suggestion["rule_id"], "RULE-WEST-B-01")
        self.assertIn("RULE-WEST-B-01", rule_suggestion["explanation"])
        kinds = {item["kind"] for item in rule_suggestion["evidence"]}
        self.assertIn("rule_scope", kinds)

    def test_case_decision_is_case_scoped(self):
        mine = self.rehearsal.review("APP-WEST-001")
        self.assertIn("CASE-WEST-001-A", mine["files"][0]["case_decision_ids"])
        other = self.rehearsal.review("APP-WEST-003")
        self.assertNotIn("CASE-WEST-001-A", other["files"][0].get("case_decision_ids", []))
        self.assertEqual(other["missing_required"], ["industry_b"])
        self.assertEqual(
            other["not_applied_decisions"][0]["decision_id"], "CASE-WEST-001-A"
        )
        self.assertIn("其他申请", other["not_applied_decisions"][0]["reason"])

    def test_unclassified_file_reports_insufficient_evidence(self):
        review = self.rehearsal.review("APP-WEST-002")
        by_id = {item["file"]["file_id"]: item for item in review["files"]}
        self.assertEqual(by_id["FILE-W-04"]["status"], "insufficient_evidence")
        self.assertEqual(
            by_id["FILE-W-04"]["reason"]["code"], "no_accepting_rule_or_declaration"
        )
        self.assertIn("FILE-W-04", review["unassigned_files"])

    def test_split_version_maps_by_declared_kind_only(self):
        review = self.rehearsal.review("APP-WEST-002")
        by_id = {item["file"]["file_id"]: item for item in review["files"]}
        self.assertEqual(by_id["FILE-W-02"]["suggestions"][0]["field"], "industry_a_1")
        self.assertEqual(by_id["FILE-W-03"]["suggestions"][0]["field"], "industry_a_2")


class RehearsalPlanTests(unittest.TestCase):
    def setUp(self):
        self.rehearsal = Rehearsal.load()

    def test_complete_application_produces_ready_plan(self):
        plan = self.rehearsal.plan("APP-WEST-001")
        self.assertEqual(plan.status, "ready_for_review")
        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [(item.field, item.placement) for item in plan.entries],
            [("industry_a", "primary"), ("industry_b", "copy")],
        )

    def test_incomplete_application_is_blocked(self):
        plan = self.rehearsal.plan("APP-WEST-002")
        self.assertEqual(plan.status, "blocked")
        codes = {item["code"] for item in plan.blockers}
        self.assertIn("required_field_unmapped", codes)
        self.assertIn("file_without_destination", codes)

    def test_confirmed_plan_is_executable_only_on_same_form(self):
        plan = confirm_plan(self.rehearsal.plan("APP-WEST-001"), "申报员-甲")
        report = preflight_report(self.rehearsal, plan, "west|lab|2026-b")
        self.assertTrue(report["portal"]["executable"])
        self.assertEqual(report["portal"]["fields"], {"industry_a": "FILE-W-01", "industry_b": "FILE-W-01"})

    def test_changed_form_invalidates_confirmed_plan(self):
        plan = confirm_plan(self.rehearsal.plan("APP-WEST-001"), "申报员-甲")
        report = preflight_report(self.rehearsal, plan, "west|lab|2026-c")
        self.assertFalse(report["portal"]["executable"])
        codes = {item["code"] for item in report["portal"]["blockers"]}
        self.assertIn("plan_invalidated", codes)
        self.assertIn("planned_field_missing", codes)
        self.assertEqual(report["portal"]["fields"], {})

    def test_unconfirmed_plan_cannot_produce_payload(self):
        plan = self.rehearsal.plan("APP-WEST-001")
        payload = portal_payload(plan, self.rehearsal.form_for("APP-WEST-001"), "csrf")
        self.assertFalse(payload["executable"])


class RehearsalImpactTests(unittest.TestCase):
    def setUp(self):
        self.rehearsal = Rehearsal.load()

    def test_version_upgrade_lists_split_and_affected_items(self):
        report = self.rehearsal.impact("west|lab|2026-b", "west|lab|2026-c")
        diff = {item["kind"]: item for item in report["diff"]["changes"]}
        self.assertIn("split", diff)
        self.assertEqual(diff["split"]["field"], "industry_a")
        self.assertEqual(diff["split"]["related"], ["industry_a_1", "industry_a_2"])
        self.assertEqual(
            [item["rule_id"] for item in report["affected_rules"]], ["RULE-WEST-B-01"]
        )
        self.assertEqual(
            [item["draft_id"] for item in report["affected_drafts"]], ["DRAFT-WEST-001"]
        )
        plan_ids = {item["plan_id"] for item in report["affected_plans"]}
        self.assertIn(self.rehearsal.plan("APP-WEST-001").plan_id, plan_ids)

    def test_impact_keeps_original_files_and_history(self):
        report = self.rehearsal.impact("west|lab|2026-b", "west|lab|2026-c")
        file_ids = {item["file_id"] for item in report["preserved"]["files"]}
        self.assertIn("FILE-W-01", file_ids)
        decision_ids = {item["decision_id"] for item in report["preserved"]["case_decisions"]}
        self.assertIn("CASE-WEST-001-A", decision_ids)
        rule_events = [
            item
            for item in report["preserved"]["ledger_events"]
            if item["subject_id"] == "RULE-WEST-B-01"
        ]
        self.assertTrue(rule_events)

    def test_draft_entries_point_at_old_fields_and_are_marked(self):
        report = self.rehearsal.impact("west|lab|2026-b", "west|lab|2026-c")
        draft = report["affected_drafts"][0]
        self.assertEqual(draft["status"], "needs_review")
        self.assertEqual(
            {item["field"] for item in draft["entries"]}, {"industry_a", "industry_b"}
        )

    def test_east_and_west_forms_are_separate(self):
        report = self.rehearsal.impact("east|lab|2026-a", "west|lab|2026-b")
        kinds = {item["kind"] for item in report["diff"]["changes"]}
        self.assertIn("removed", kinds)
        self.assertIn("added", kinds)


class DrillSubmissionTests(unittest.TestCase):
    """门户提交只用于本地演练端点，且必须显式调用。"""

    def test_submit_refuses_invalidated_plan_without_network_call(self):
        rehearsal = Rehearsal.load()
        plan = confirm_plan(rehearsal.plan("APP-WEST-001"), "申报员")
        with serve(load_origin_scenario()) as origin:
            from permit_forms.drill import submit

            result = submit(rehearsal, plan, "west|lab|2026-c", origin.base_url, "form-2026-b")
            self.assertFalse(result["submitted"])
            self.assertEqual(len(origin.requests), 0)

    def test_submit_on_same_form_uses_local_portal(self):
        rehearsal = Rehearsal.load()
        plan = confirm_plan(rehearsal.plan("APP-WEST-001"), "申报员")
        origin_scenario = load_origin_scenario()
        with serve(origin_scenario) as origin:
            from permit_forms.drill import submit

            result = submit(rehearsal, plan, "west|lab|2026-b", origin.base_url, origin_scenario["portal"]["csrf"])
            self.assertTrue(result["submitted"])
            self.assertEqual(result["response"]["id"], "APP-WEST-001")
            self.assertEqual(len(origin.requests), 1)


class DriftRehearsalTests(unittest.TestCase):
    def test_rehearsal_review_can_be_acknowledged_and_compared(self):
        from permit_forms.mapping import drift_since

        rehearsal = Rehearsal.load()
        form = rehearsal.form_for("APP-WEST-001")
        review = rehearsal.review_object("APP-WEST-001")
        rehearsal.ledger.acknowledge_review("APP-WEST-001", form, review, "申报员-甲")
        drift = drift_since(review, rehearsal.ledger.latest_acknowledgement("APP-WEST-001"))
        self.assertFalse(drift["requires_review"])
        self.assertEqual(drift["detail"], "建议与上次确认一致。")


class PersistenceTests(unittest.TestCase):
    def test_rehearsal_reloads_snapshot_without_duplicating_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.sqlite3"
            first = Rehearsal.load(ledger=Ledger(path))
            first.ledger.close()
            second = Rehearsal.load(ledger=Ledger(path))
            try:
                self.assertEqual(len(second.ledger.rules()), 1)
                self.assertEqual(len(second.ledger.case_decisions()), 1)
                self.assertEqual(len(second.ledger.events()), 2)
            finally:
                second.ledger.close()


if __name__ == "__main__":
    unittest.main()
