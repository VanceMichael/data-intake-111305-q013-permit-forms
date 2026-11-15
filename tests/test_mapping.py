import unittest

from permit_forms.forms import FormField, FormVersion, Scope, parse_form_version
from permit_forms.mapping import (
    PLACEMENTS,
    CaseDecision,
    MappingRule,
    MaterialFile,
    explain_missing,
    review_mapping,
)


def form(version="2026-b", district="west", application_type="lab", fields=None):
    return FormVersion(
        district,
        application_type,
        version,
        tuple(
            FormField(**item)
            for item in (
                fields
                if fields is not None
                else [
                    {"name": "industry_a", "label": "行业资质一", "accepts": ("industry_cert",)},
                    {"name": "industry_b", "label": "行业资质二"},
                ]
            )
        ),
    )


def material(file_id="F1", kind="industry_cert", **extra):
    return MaterialFile(
        file_id, file_id + ".pdf", "sha256:" + "1" * 64, kind, **extra
    )


class ScopeTests(unittest.TestCase):
    def test_scope_requires_all_three_parts(self):
        for name in ("district", "application_type", "form_version"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Scope(**{name: " ", **{other: "x" for other in ("district", "application_type", "form_version") if other != name}})

    def test_scope_of_form(self):
        target = form()
        self.assertEqual(Scope.of(target).key, "west|lab|2026-b")


class RuleScopeTests(unittest.TestCase):
    def test_rule_applies_within_exact_scope_only(self):
        target = form()
        rule = MappingRule("R1", Scope.of(target), "industry_a", ("industry_cert",), "manual", "乙区规则")
        review = review_mapping(target, [material()], [rule], application_id="A1")
        suggestion = review.files[0].suggestions[0]
        self.assertEqual(suggestion.rule_id, "R1")
        self.assertEqual(suggestion.field, "industry_a")
        # 不同地区、不同版本都不应命中规则；即使字段声明接受该材料，
        # 结果里也不能出现这条规则。
        for other in (form(district="east"), form(version="2026-c")):
            with self.subTest(scope=other.key):
                second = review_mapping(other, [material()], [rule], application_id="A1")
                origins = {item.origin for item in second.files[0].suggestions}
                self.assertNotIn("manual_rule", origins)
                self.assertNotIn("R1", {item.rule_id for item in second.files[0].suggestions})

    def test_other_version_rule_is_reported_as_not_applied(self):
        rule = MappingRule("R1", Scope("west", "lab", "2026-b"), "industry_a", ("industry_cert",), "manual", "旧版规则")
        target = form(version="2026-c", fields=[{"name": "industry_a", "label": "一"}])
        review = review_mapping(target, [material()], [rule], application_id="A1")
        self.assertEqual(review.files[0].status, "insufficient_evidence")
        self.assertEqual(review.files[0].reason["code"], "no_rule_for_current_version")
        self.assertEqual(
            review.files[0].reason["rules_not_applied"][0]["rule_id"], "R1"
        )
        self.assertTrue(
            review.files[0].reason["rules_not_applied"][0]["reason"].startswith("表单版本不同")
        )

    def test_inactive_rule_is_not_applied(self):
        target = form(fields=[{"name": "industry_a", "label": "一"}])
        rule = MappingRule(
            "R1", Scope.of(target), "industry_a", ("industry_cert",), "manual", "停用", active=False
        )
        review = review_mapping(target, [material()], [rule], application_id="A1")
        self.assertEqual(review.files[0].status, "insufficient_evidence")


class CaseDecisionTests(unittest.TestCase):
    def test_case_decision_only_applies_to_its_application(self):
        target = form()
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope.of(target), "assign", "窗口核验", target_field="industry_b"
        )
        mine = review_mapping(target, [material()], [], [decision], "APP-1")
        self.assertEqual(mine.files[0].status, "suggested")
        chosen = {item.field: item for item in mine.files[0].suggestions}
        self.assertEqual(chosen["industry_b"].origin, "case_decision")
        self.assertEqual(chosen["industry_b"].placement, "primary")
        # 未被裁定的字段仍走字段声明，没有被例外顺带改写
        self.assertEqual(chosen["industry_a"].origin, "declared")
        other = review_mapping(target, [material()], [], [decision], "APP-2")
        self.assertEqual(other.files[0].case_decision_ids, ())
        self.assertNotIn(
            "case_decision", {item.origin for item in other.files[0].suggestions}
        )
        self.assertEqual(other.not_applied_decisions[0]["decision_id"], "D1")
        self.assertIn("其他申请", other.not_applied_decisions[0]["reason"])

    def test_case_decision_is_not_carried_to_another_form_version(self):
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope("west", "lab", "2026-b"), "assign", "x", target_field="industry_b"
        )
        new_form = form(version="2026-c")
        review = review_mapping(new_form, [material()], [], [decision], "APP-1")
        self.assertEqual(review.files[0].case_decision_ids, ())
        self.assertEqual(review.not_applied_decisions[0]["decision_id"], "D1")
        self.assertIn("作用域不同", review.not_applied_decisions[0]["reason"])

    def test_case_decision_cannot_be_used_without_application_identity(self):
        with self.assertRaises(ValueError):
            review_mapping(
                form(),
                [material()],
                [],
                [CaseDecision("D1", "APP-1", "F1", Scope("west", "lab", "2026-b"), "assign", "x", target_field="industry_a")],
            )

    def test_reject_decision_excludes_file_without_mapping_it(self):
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope("west", "lab", "2026-b"), "reject", "重复件"
        )
        review = review_mapping(form(), [material()], [], [decision], "APP-1")
        self.assertEqual(review.files[0].status, "excluded_by_case_decision")
        self.assertEqual(review.files[0].suggestions, ())
        self.assertEqual(review.files[0].case_decision_ids, ("D1",))

    def test_case_target_missing_in_new_form_is_not_silently_reused(self):
        new_form = form(version="2026-c", fields=[{"name": "industry_x", "label": "新字段"}])
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope("west", "lab", "2026-c"), "assign", "x", target_field="industry_a"
        )
        review = review_mapping(new_form, [material()], [], [decision], "APP-1")
        self.assertEqual(review.files[0].status, "insufficient_evidence")
        self.assertEqual(review.files[0].reason["code"], "case_decision_target_missing")
        self.assertEqual(review.files[0].reason["missing"], ["field:industry_a"])
        self.assertIn("D1", {item["id"] for item in review.files[0].reason["rejected_candidates"]})

    def test_case_decision_overrides_rule_for_same_field_only(self):
        target = form()
        rule = MappingRule("R1", Scope.of(target), "industry_a", ("industry_cert",), "manual", "规则")
        decision = CaseDecision(
            "D1", "APP-1", "F1", Scope.of(target), "assign", "本笔例外", target_field="industry_a", placement="primary"
        )
        review = review_mapping(target, [material()], [rule], [decision], "APP-1")
        suggestion = review.files[0].suggestions[0]
        self.assertEqual(suggestion.origin, "case_decision")
        self.assertEqual(suggestion.source_decision_id, "D1")
        rejected = {item.identifier for item in review.files[0].rejected_candidates}
        self.assertIn("R1", rejected)


class ExplainabilityTests(unittest.TestCase):
    def test_suggestion_carries_evidence_and_scope(self):
        target = form()
        suggestion = review_mapping(target, [material()], application_id="A1").files[0].suggestions[0]
        kinds = {item.kind for item in suggestion.evidence}
        self.assertIn("form_declaration", kinds)
        self.assertIn("declared_material_kind", kinds)
        self.assertIn("target_field", kinds)
        self.assertEqual(suggestion.scope_key, target.key)
        self.assertIn("industry_a", suggestion.explanation)

    def test_insufficient_evidence_states_missing_proof(self):
        target = form(fields=[{"name": "only_other", "label": "其他", "accepts": ("other_kind",)}])
        review = review_mapping(target, [material(kind="mystery")], application_id="A1")
        self.assertEqual(review.files[0].status, "insufficient_evidence")
        self.assertEqual(review.files[0].reason["code"], "no_accepting_rule_or_declaration")
        self.assertIn("mystery", review.files[0].reason["missing"][1])
        self.assertTrue(explain_missing(review))

    def test_scope_mismatched_file_is_explained_not_guessed(self):
        target = form(district="west")
        file = material(districts=("east",))
        review = review_mapping(target, [file], application_id="A1")
        self.assertEqual(review.files[0].reason["code"], "file_scope_mismatch")
        self.assertIn("file_district:west", review.files[0].reason["missing"])

    def test_names_are_not_used_to_guess_target(self):
        target = form(fields=[{"name": "industry_cert_field", "label": "行业资质"}])
        review = review_mapping(target, [material(kind="industry_cert")], application_id="A1")
        self.assertEqual(review.files[0].status, "insufficient_evidence")

    def test_multiple_targets_get_single_primary_and_copies(self):
        target = form(fields=[
            {"name": "industry_a", "label": "一", "accepts": ("industry_cert",)},
            {"name": "industry_b", "label": "二", "accepts": ("industry_cert",)},
        ])
        review = review_mapping(target, [material()], application_id="A1")
        placements = [item.placement for item in review.files[0].suggestions]
        self.assertEqual(placements.count("primary"), 1)
        self.assertEqual(placements.count("copy"), 1)

    def test_two_primary_targets_are_a_conflict(self):
        target = form()
        rules = [
            MappingRule("R1", Scope.of(target), "industry_a", ("industry_cert",), "manual", "A"),
            MappingRule("R2", Scope.of(target), "industry_b", ("industry_cert",), "manual", "B"),
        ]
        review = review_mapping(target, [material()], rules, application_id="A1")
        self.assertEqual(review.files[0].status, "conflicting")
        self.assertEqual(review.files[0].reason["code"], "multiple_primary_targets")
        self.assertEqual(review.rule_conflicts[0]["material_kind"], "industry_cert")
        self.assertTrue(review.rule_conflicts[0]["affects_this_application"])
        self.assertEqual(review.unassigned_files, ("F1",))


class ValidationTests(unittest.TestCase):
    def test_duplicate_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate file"):
            review_mapping(form(), [material(), material()], application_id="A1")

    def test_conflicting_case_decisions_are_rejected(self):
        scope = Scope("west", "lab", "2026-b")
        decisions = [
            CaseDecision("D1", "APP-1", "F1", scope, "reject", "排除"),
            CaseDecision("D2", "APP-1", "F1", scope, "assign", "指定", target_field="industry_a"),
        ]
        with self.assertRaisesRegex(ValueError, "conflicting case decisions"):
            review_mapping(form(), [material()], [], decisions, "APP-1")

    def test_rule_requires_explicit_material_types(self):
        with self.assertRaises(ValueError):
            MappingRule("R1", Scope("west", "lab", "2026-b"), "industry_a", (), "manual", "x")

    def test_parse_form_version_validates_fields(self):
        with self.assertRaises(ValueError):
            parse_form_version({"district": "west", "application_type": "lab", "version": "v", "fields": [{"name": ""}]})


if __name__ == "__main__":
    unittest.main()
