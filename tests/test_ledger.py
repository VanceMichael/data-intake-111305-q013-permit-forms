import json
import tempfile
import unittest
from pathlib import Path

from permit_forms.forms import FormField, FormVersion, Scope
from permit_forms.ledger import Ledger
from permit_forms.mapping import (
    CaseDecision,
    MappingRule,
    MaterialFile,
    drift_since,
    review_mapping,
)


def form(version="2026-b"):
    return FormVersion(
        "west",
        "lab",
        version,
        (
            FormField("industry_a", "行业资质一", accepts=("industry_cert",)),
            FormField("industry_b", "行业资质二"),
        ),
    )


def material(file_id="F1", kind="industry_cert"):
    return MaterialFile(file_id, file_id + ".pdf", "sha256:" + "1" * 64, kind)


def rule(rule_id="R1", target="industry_a", version="2026-b", accepts=("industry_cert",)):
    return MappingRule(
        rule_id, Scope("west", "lab", version), target, accepts, "manual", "规则 " + rule_id
    )


def decision(decision_id="D1", application="APP-1", target="industry_b"):
    return CaseDecision(
        decision_id, application, "F1", Scope("west", "lab", "2026-b"), "assign", "个别情况", target_field=target
    )


class LedgerTests(unittest.TestCase):
    def test_events_are_append_only_and_explainable(self):
        ledger = Ledger()
        ledger.add_rule(rule(), "alice", "首次建立")
        ledger.add_rule(rule("R2", target="industry_b"), "bob", "补充规则")
        self.assertEqual([item.kind for item in ledger.events()], ["rule_created"] * 2)
        history = ledger.explain("R1")
        self.assertEqual(history["kind"], "rule_created")
        self.assertEqual(history["revisions"][0]["actor"], "alice")
        self.assertEqual(history["current"]["rule"]["target_field"], "industry_a")

    def test_case_decision_and_rule_are_distinct_kinds(self):
        ledger = Ledger()
        ledger.add_case_decision(decision(), "alice", "本笔特殊")
        self.assertEqual(ledger.rules(), ())
        self.assertEqual(len(ledger.case_decisions("APP-1")), 1)
        self.assertEqual(ledger.case_decisions("APP-2"), ())

    def test_case_decision_cannot_be_replaced_silently(self):
        ledger = Ledger()
        ledger.add_case_decision(decision("D1"), "alice", "x")
        with self.assertRaisesRegex(ValueError, "already covers"):
            ledger.add_case_decision(decision("D2"), "alice", "y")

    def test_promote_requires_explicit_material_kind(self):
        ledger = Ledger()
        ledger.add_case_decision(decision(), "alice", "x")
        with self.assertRaises(TypeError):
            ledger.promote_case("D1", "R9", "alice", "推广")
        with self.assertRaisesRegex(ValueError, "explicit accepts"):
            ledger.promote_case("D1", "R9", "alice", "推广", accepts=())

    def test_promotion_creates_rule_but_keeps_case_scoped(self):
        ledger = Ledger()
        ledger.add_case_decision(decision(), "alice", "x")
        ledger.promote_case("D1", "R9", "bob", "重复出现", accepts=("industry_cert",))
        promoted = {item.rule_id: item for item in ledger.rules()}["R9"]
        self.assertEqual(promoted.source_decision_id, "D1")
        self.assertEqual(promoted.origin, "manual")
        # 原例外仍只作用于原申请
        mine = review_mapping(form(), [material()], ledger.rules(), ledger.case_decisions("APP-1"), "APP-1")
        origins = {item.origin for item in mine.files[0].suggestions}
        self.assertIn("case_decision", origins)
        other = review_mapping(form(), [material()], ledger.rules(), ledger.case_decisions("APP-2"), "APP-2")
        self.assertEqual(other.files[0].case_decision_ids, ())
        self.assertIn("manual_rule", {item.origin for item in other.files[0].suggestions})
        self.assertEqual(ledger.history("D1")[0].kind, "case_decided")
        self.assertEqual(ledger.history("R9")[0].kind, "rule_promoted")

    def test_deactivation_is_recorded_not_deleted(self):
        ledger = Ledger()
        ledger.add_rule(rule(), "alice", "x")
        ledger.deactivate_rule("R1", "bob", "材料类型废止")
        self.assertEqual(ledger.rules(), ())
        versions = {item.rule_id: item for item in ledger.all_rule_versions()}["R1"]
        self.assertFalse(versions.active)
        self.assertEqual(
            [item.kind for item in ledger.history("R1")], ["rule_created", "rule_deactivated"]
        )

    def test_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.sqlite3"
            ledger = Ledger(path)
            ledger.add_rule(rule(), "alice", "x")
            ledger.add_case_decision(decision(), "alice", "y")
            ledger.close()
            reopened = Ledger(path)
            try:
                self.assertEqual([item.rule_id for item in reopened.rules()], ["R1"])
                self.assertEqual([item.decision_id for item in reopened.case_decisions()], ["D1"])
                self.assertEqual(len(reopened.events()), 2)
            finally:
                reopened.close()

    def test_acknowledgement_is_immutable_and_reusable(self):
        ledger = Ledger()
        target = form()
        review = review_mapping(target, [material()], application_id="APP-1")
        event = ledger.acknowledge_review("APP-1", target, review, "申报员")
        ack = ledger.latest_acknowledgement("APP-1")
        self.assertEqual(ack.ack_id, event.subject_id)
        with self.assertRaises(ValueError):
            ledger.acknowledge_review("APP-1", target, review, "申报员")


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.target = form()
        self.review = review_mapping(self.target, [material()], application_id="APP-1")
        self.ledger.acknowledge_review("APP-1", self.target, self.review, "申报员")

    def test_unchanged_review_does_not_require_new_review(self):
        drift = drift_since(self.review, self.ledger.latest_acknowledgement("APP-1"))
        self.assertFalse(drift["requires_review"])

    def test_new_rule_changes_destination_and_flags_drift(self):
        self.ledger.add_rule(rule("R1", target="industry_b"), "alice", "新规则")
        refreshed = review_mapping(
            self.target, [material()], self.ledger.rules(), application_id="APP-1"
        )
        drift = drift_since(refreshed, self.ledger.latest_acknowledgement("APP-1"))
        self.assertTrue(drift["requires_review"])
        self.assertTrue(drift["added"])
        self.assertTrue(drift["removed"])
        removed = {(item["field"], item["placement"]) for item in drift["removed"]}
        added = {(item["field"], item["placement"]) for item in drift["added"]}
        self.assertIn(("industry_a", "primary"), removed)
        self.assertIn(("industry_b", "primary"), added)
        # 原先的主归档降级为复制归档，也属于需要重新确认的变化
        self.assertIn(("industry_a", "copy"), added)

    def test_form_change_invalidates_previous_acknowledgement(self):
        changed = FormVersion(
            "west", "lab", "2026-c", (FormField("industry_x", "新字段"),)
        )
        review = review_mapping(changed, [material()], application_id="APP-1")
        drift = drift_since(review, self.ledger.latest_acknowledgement("APP-1"))
        self.assertTrue(drift["requires_review"])
        self.assertFalse(drift["same_form"])

    def test_without_acknowledgement_review_is_required(self):
        self.assertTrue(drift_since(self.review, None)["requires_review"])


if __name__ == "__main__":
    unittest.main()
