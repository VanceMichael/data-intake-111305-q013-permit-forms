import unittest

from permit_forms.forms import FormField, FormVersion, Scope
from permit_forms.mapping import (
    CaseDecision,
    MappingRule,
    MaterialFile,
    review_mapping,
)
from permit_forms.plan import (
    build_plan,
    confirm_plan,
    portal_payload,
    preflight,
)


def form(version="2026-b", extra=()):
    return FormVersion(
        "west",
        "lab",
        version,
        (
            FormField("industry_a", "行业资质一", accepts=("industry_cert",)),
            FormField("industry_b", "行业资质二"),
        )
        + tuple(extra),
    )


def material(file_id="F1", kind="industry_cert"):
    return MaterialFile(file_id, file_id + ".pdf", "sha256:" + "1" * 64, kind)


def ready_plan():
    target = form()
    decision = CaseDecision(
        "D1", "APP-1", "F1", Scope.of(target), "assign", "窗口核验", target_field="industry_b"
    )
    review = review_mapping(target, [material()], [], [decision], "APP-1")
    return target, build_plan(review)


class PlanTests(unittest.TestCase):
    def test_plan_is_generated_without_submitting(self):
        target, plan = ready_plan()
        self.assertEqual(plan.status, "ready_for_review")
        self.assertTrue(plan.executable is False)  # 未确认前不可执行
        self.assertEqual(
            [(item.field, item.placement) for item in plan.entries],
            [("industry_a", "copy"), ("industry_b", "primary")],
        )

    def test_missing_required_field_blocks_plan_and_confirmation(self):
        target = form()
        review = review_mapping(target, [material()], application_id="APP-1")
        plan = build_plan(review)
        self.assertEqual(plan.status, "blocked")
        self.assertIn("industry_b", {item["field"] for item in plan.blockers})
        with self.assertRaises(ValueError):
            confirm_plan(plan, "申报员")

    def test_unassigned_file_blocks_plan(self):
        target = form()
        review = review_mapping(
            target, [material(), material("F9", "mystery")], application_id="APP-1"
        )
        plan = build_plan(review)
        self.assertIn("F9", {item.get("file_id") for item in plan.blockers})

    def test_confirmation_binds_form_and_content(self):
        target, plan = ready_plan()
        confirmed = confirm_plan(plan, "申报员-甲")
        self.assertTrue(confirmed.executable)
        self.assertEqual(confirmed.confirmed_by, "申报员-甲")
        self.assertNotEqual(confirmed.confirm_token, plan.confirm_token)
        self.assertEqual(confirm_plan(plan, "申报员-乙").confirm_token, confirmed.confirm_token)

    def test_confirm_requires_actor(self):
        target, plan = ready_plan()
        with self.assertRaises(ValueError):
            confirm_plan(plan, " ")

    def test_portal_payload_refuses_unconfirmed_plan(self):
        target, plan = ready_plan()
        payload = portal_payload(plan, target, "csrf")
        self.assertFalse(payload["executable"])
        self.assertIn("plan_unconfirmed", {item["code"] for item in payload["blockers"]})
        self.assertEqual(payload["fields"], {})
        self.assertIsNone(payload["csrf"])

    def test_portal_payload_contains_entries_after_confirmation(self):
        target, plan = ready_plan()
        confirmed = confirm_plan(plan, "申报员")
        payload = portal_payload(confirmed, target, "csrf")
        self.assertTrue(payload["executable"])
        self.assertEqual(payload["fields"], {"industry_a": "F1", "industry_b": "F1"})

    def test_same_version_but_changed_structure_invalidates_plan(self):
        target, plan = ready_plan()
        confirmed = confirm_plan(plan, "申报员")
        changed = FormVersion(
            "west", "lab", "2026-b", (FormField("industry_a", "行业资质一"),)
        )
        checked = preflight(confirmed, changed)
        self.assertIsNotNone(checked.invalidated_at)
        self.assertEqual(checked.invalidation["code"], "form_structure_changed")
        self.assertEqual(checked.invalidation["missing_fields"], ["industry_b"])

    def test_version_change_invalidates_confirmed_plan(self):
        target, plan = ready_plan()
        confirmed = confirm_plan(plan, "申报员")
        payload = portal_payload(confirmed, form(version="2026-c"), "csrf")
        self.assertFalse(payload["executable"])
        codes = {item["code"] for item in payload["blockers"]}
        self.assertIn("plan_invalidated", codes)
        self.assertEqual(payload["fields"], {})

    def test_missing_original_file_blocks_execution(self):
        target, plan = ready_plan()
        confirmed = confirm_plan(plan, "申报员")
        payload = portal_payload(confirmed, target, "csrf", file_by_id={})
        self.assertFalse(payload["executable"])
        self.assertIn("planned_file_missing", {item["code"] for item in payload["blockers"]})

    def test_required_met_by_current_form_but_not_by_plan(self):
        target = form(extra=(FormField("industry_c", "行业资质三"),))
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope.of(target), "assign", "x", target_field="industry_b"
        )
        review = review_mapping(target, [material()], [], [decision], "APP-1")
        plan = build_plan(review)
        self.assertEqual(plan.status, "blocked")
        confirmed_payload = portal_payload(plan, target, "csrf")
        fields = {item.get("field") for item in confirmed_payload["blockers"]}
        self.assertIn("industry_c", fields)


if __name__ == "__main__":
    unittest.main()
