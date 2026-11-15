import unittest

from permit_forms.forms import FormField, FormVersion, diff_forms, parse_form_version
from permit_forms.impact import Draft, DraftEntry, analyse_impact
from permit_forms.ledger import Ledger
from permit_forms.mapping import CaseDecision, MappingRule, MaterialFile, review_mapping
from permit_forms.plan import build_plan, confirm_plan


def version_c():
    return FormVersion(
        "west",
        "lab",
        "2026-c",
        (
            FormField("industry_a_1", "行业资质一（第一部分）", accepts=("industry_cert_part_a",), split_of="industry_a"),
            FormField("industry_a_2", "行业资质一（第二部分）", accepts=("industry_cert_part_b",), split_of="industry_a"),
            FormField("industry_b", "行业资质二", accepts=("industry_cert",)),
        ),
    )


def version_b():
    return FormVersion(
        "west",
        "lab",
        "2026-b",
        (
            FormField("industry_a", "行业资质一", accepts=("industry_cert",)),
            FormField("industry_b", "行业资质二"),
        ),
    )


def material(file_id="F1", kind="industry_cert"):
    return MaterialFile(file_id, file_id + ".pdf", "sha256:" + "1" * 64, kind)


class DiffTests(unittest.TestCase):
    def test_split_is_reported_only_when_declared(self):
        result = diff_forms(version_b(), version_c())
        kinds = {item.kind: item for item in result.changes}
        self.assertIn("split", kinds)
        self.assertEqual(kinds["split"].field, "industry_a")
        self.assertEqual(kinds["split"].related, ("industry_a_1", "industry_a_2"))
        # industry_b 保留但接受类型变化
        self.assertIn("retyped", kinds)

    def test_undeclared_rename_is_added_and_removed_not_guessed(self):
        before = FormVersion("west", "lab", "v1", (FormField("old_name", "旧"),))
        after = FormVersion("west", "lab", "v2", (FormField("new_name", "新"),))
        kinds = {item.kind for item in diff_forms(before, after).changes}
        self.assertEqual(kinds, {"removed", "added"})

    def test_declared_rename_is_reported_as_rename(self):
        before = FormVersion("west", "lab", "v1", (FormField("old_name", "旧"),))
        after = FormVersion("west", "lab", "v2", (FormField("new_name", "新", renamed_from="old_name"),))
        changes = diff_forms(before, after).changes
        self.assertEqual([item.kind for item in changes], ["renamed"])
        self.assertEqual(changes[0].field, "old_name")
        self.assertEqual(changes[0].related, ("new_name",))

    def test_required_change_is_reported(self):
        before = FormVersion("west", "lab", "v1", (FormField("a", "A", required=True),))
        after = FormVersion("west", "lab", "v2", (FormField("a", "A", required=False),))
        self.assertEqual(
            [item.kind for item in diff_forms(before, after).changes], ["required_changed"]
        )


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.before = version_b()
        self.after = version_c()
        self.ledger = Ledger()
        self.rule = MappingRule(
            "R1", self.before_scope(), "industry_a", ("industry_cert",), "manual", "乙区规则"
        )
        self.ledger.add_rule(self.rule, "alice", "建立")

    def before_scope(self):
        from permit_forms.forms import Scope

        return Scope.of(self.before)

    def test_rule_on_split_field_is_flagged_and_not_migrated(self):
        report = analyse_impact(
            self.before, self.after, rules=self.ledger.all_rule_versions(), ledger=self.ledger
        )
        self.assertEqual([item["rule_id"] for item in report.affected_rules], ["R1"])
        self.assertIn("未自动迁移", report.affected_rules[0]["reason"])
        impact = {item.field: item for item in report.impacts}
        self.assertEqual(impact["industry_a"].change_kind, "split")
        self.assertEqual(impact["industry_a"].affected_rules, ("R1",))
        self.assertEqual(impact["industry_a"].related, ("industry_a_1", "industry_a_2"))

    def test_history_is_preserved_in_report(self):
        report = analyse_impact(
            self.before, self.after, rules=self.ledger.all_rule_versions(), ledger=self.ledger
        )
        self.assertEqual(report.affected_rules[0]["history"][0]["kind"], "rule_created")
        self.assertIn("files", report.preserved)
        self.assertIn("rule_versions", report.preserved)

    def test_affected_plans_and_drafts_are_marked_not_rewritten(self):
        review = review_mapping(
            self.before,
            [material()],
            [self.rule],
            [CaseDecision("D1", "APP-1", "F1", self.before_scope(), "assign", "x", target_field="industry_b", placement="copy")],
            "APP-1",
        )
        plan = confirm_plan(build_plan(review), "申报员")
        draft = Draft(
            "DRAFT-1",
            "APP-1",
            self.before.key,
            (DraftEntry("industry_a", "F1"), DraftEntry("industry_b", "F1")),
        )
        report = analyse_impact(
            self.before,
            self.after,
            rules=[self.rule],
            plans=[plan],
            drafts=[draft],
            files=[material()],
        )
        self.assertEqual(
            [item["plan_id"] for item in report.affected_plans], [plan.plan_id]
        )
        self.assertTrue(report.affected_plans[0]["confirmed"])
        self.assertEqual([item["draft_id"] for item in report.affected_drafts], ["DRAFT-1"])
        statuses = {
            item["field"]: item["status"] for item in report.affected_drafts[0]["entries"]
        }
        self.assertEqual(statuses["industry_a"], "needs_review")
        self.assertEqual(report.preserved["plan_snapshots"][0]["plan_id"], plan.plan_id)
        # 原草稿内容保持不变
        self.assertEqual(report.preserved["draft_snapshots"][0]["entries"][0]["field"], "industry_a")

    def test_deleted_field_is_flagged_for_rules_plans_and_drafts(self):
        after = FormVersion(
            "west",
            "lab",
            "2026-d",
            (
                FormField("industry_a", "行业资质一", accepts=("industry_cert",)),
                FormField("industry_c", "行业资质三"),
            ),
        )
        review = review_mapping(
            self.before,
            [material()],
            [self.rule],
            [CaseDecision("D1", "APP-1", "F1", self.before_scope(), "assign", "x", target_field="industry_b", placement="copy")],
            "APP-1",
        )
        plan = confirm_plan(build_plan(review), "申报员")
        draft = Draft(
            "DRAFT-1",
            "APP-1",
            self.before.key,
            (DraftEntry("industry_a", "F1"), DraftEntry("industry_b", "F1", "copy")),
        )
        report = analyse_impact(
            self.before, after, rules=[self.rule], plans=[plan], drafts=[draft], files=[material()]
        )
        by_field = {item.field: item for item in report.impacts}
        self.assertEqual(by_field["industry_b"].change_kind, "removed")
        self.assertEqual(by_field["industry_b"].affected_plans, (plan.plan_id,))
        self.assertEqual(by_field["industry_b"].affected_drafts, ("DRAFT-1",))
        statuses = {
            item["field"]: item["status"] for item in report.affected_drafts[0]["entries"]
        }
        self.assertEqual(statuses["industry_b"], "needs_review")
        # 被删除字段的引用仍保留在快照里可查
        self.assertEqual(
            report.preserved["draft_snapshots"][0]["entries"][1]["field"], "industry_b"
        )

    def test_plans_on_other_versions_are_not_listed(self):
        other = FormVersion("east", "lab", "2026-a", (FormField("business_licence", "许可"),))
        review = review_mapping(other, [material("E1", "business_licence")], application_id="APP-E")
        plan = build_plan(review)
        report = analyse_impact(self.before, self.after, plans=[plan])
        self.assertEqual(report.affected_plans, ())

    def test_no_changes_means_no_impacts(self):
        report = analyse_impact(self.before, self.before)
        self.assertEqual(report.impacts, ())
        self.assertEqual(report.diff.changes, ())

    def test_parse_declared_split_from_source_record(self):
        raw = {
            "district": "west",
            "application_type": "lab",
            "version": "2026-c",
            "fields": [
                {"name": "industry_a_1", "label": "一", "split_of": "industry_a"},
                {"name": "industry_a_2", "label": "二", "split_of": "industry_a"},
            ],
        }
        parsed = parse_form_version(raw)
        self.assertEqual(parsed.field("industry_a_1").split_of, "industry_a")


if __name__ == "__main__":
    unittest.main()
