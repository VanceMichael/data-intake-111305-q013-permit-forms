# 材料映射、填报计划与版本变化

本文说明在原始接收之上新增的业务层：如何把同一份行业证明按地区、申报类别、
表单版本映射到不同字段，如何解释每条建议，如何把项目内例外与可复用规则分开，
以及表单升级时旧映射和草稿如何被标出影响。所有资料均为本地合成，门户端点只
用于离线演练。

## 1. 作用域

映射只认三个值完全相同的作用域：

```
地区 district | 申报类别 application_type | 表单版本 form_version
```

任何一项不同都不套用。乙区表单把行业证明拆成两个字段，甲区叫经营许可，这属于
两个不同作用域，各自有各自的规则和字段声明，不会互相借用。

来源记录里的 `accepts`、`split_of`、`merged_from`、`renamed_from` 是材料类型
与结构关系的唯一依据。**不比较字段名称是否相似，也不使用相似度或机器学习推断。**

## 2. 三种建议来源

| 来源 | 生效范围 | 说明 |
| --- | --- | --- |
| 表单字段声明 `declared` | 当前作用域 | 字段声明 `accepts` 含该材料类型 |
| 可复用规则 `manual_rule` | 当前作用域的所有申请 | 由人工显式创建，带规则编号与说明 |
| 项目内例外 `case_decision` | 一笔申请 + 一份文件 + 一个版本 | 人工针对个案裁定 |

优先级：项目内例外 > 可复用规则 > 字段声明。例外只覆盖它点名的字段，不会把同
一文件在其他字段上的规则或声明一并抹掉。

每条建议都带：

```json
{
  "field": "industry_a",
  "placement": "primary",
  "origin": "manual_rule",
  "rule_id": "RULE-WEST-B-01",
  "scope": "west|lab|2026-b",
  "explanation": "文件 FILE-W-01 声明材料类型 industry_cert，命中规则 …",
  "evidence": [
    {"kind": "rule", "value": "RULE-WEST-B-01", "source": "可复用规则（来源：manual）"},
    {"kind": "rule_scope", "value": "west|lab|2026-b", "source": "规则作用域与当前表单完全一致"},
    {"kind": "declared_material_kind", "value": "industry_cert", "source": "文件 FILE-W-01 声明类型"},
    {"kind": "target_field", "value": "industry_a", "source": "表单 west|lab|2026-b 存在该字段"}
  ]
}
```

`placement` 只有 `primary`（主归档）和 `copy`（复制归档）。同一文件落到多个字段
时最多一个主归档；出现两个主归档即记为冲突，等待人工裁定。

## 3. 明确说“证据不足”

无法给出建议时状态是 `insufficient_evidence`，并返回 `reason`：

- `no_accepting_rule_or_declaration`：没有规则或字段声明接受该材料类型；
- `no_rule_for_current_version`：只有其他版本的规则接受，历史规则**未**套用；
- `file_scope_mismatch`：文件声明的地区/申报类别与当前作用域不符；
- `rule_not_usable`：有规则接受材料类型，但目标字段在当前版本不存在；
- `case_decision_target_missing`：人工裁定指向的字段已被新版本删除。

`rejected_candidates` 会列出被考虑但未采用的候选及原因（例如“作用域不同，未套
用”“本笔申请的人工裁定优先”），`missing` 列出缺少的证据键。申报员看到的是原因，
不是猜测。

## 4. 项目内例外与可复用规则

两者是记录里的不同事件，不能互相代替：

- `Ledger.add_case_decision(...)` 写入 `case_decided`，只对
  `(application_id, file_id, scope)` 这一组合生效；
- `Ledger.promote_case(...)` 才会写入 `rule_promoted`，且必须显式给出
  `accepts`（材料类型），不能从单笔特例推断；
- 提升后原例外保持不变，仍只对原申请生效，新规则带 `source_decision_id`
  指回原例外；
- 停用规则写入 `rule_deactivated`，历史版本仍可查。

同一申请、同一文件、同一作用域不允许出现互相矛盾的例外（既排除又指定），也不
允许默默覆盖；修正必须新增记录。

`Ledger.history(id)` / `Ledger.explain(id)` 返回该规则或例外的完整事件链。SQLite
快照只追加事件，重开后可重放。

## 5. 刷新与人工确认

`Ledger.acknowledge_review(...)` 保存申报员对一次审阅结果的快照（字段 → 文件 →
主/复制归档 → 来源）。下次来源刷新后，`drift_since(review, acknowledgement)`
比较当前建议与快照：

- 一致：`requires_review` 为 false；
- 有新增、移除或去向变化：列为 `added` / `removed` / `changed`，要求重新确认；
- 表单指纹不同：直接判定上次确认不能沿用。

这样人工确认过一次的建议不会被下次刷新静默覆盖。

## 6. 填报计划

`build_plan(review)` 生成计划，状态只有两种：

- `ready_for_review`：所有必填字段都有材料，也没有文件悬空；
- `blocked`：列出阻断项（`required_field_unmapped`、`file_without_destination`、
  `conflicting_rules`）。

`blocked` 计划不能确认，`confirm_plan` 会直接拒绝。确认令牌绑定表单指纹与计划内容，
不产生提交动作。

## 7. 执行前必须重新校验

`portal_payload(plan, current_form, csrf)` 只生成将要提交的数据，**不发请求**。
它先做与 `preflight` 相同的检查：

- 表单作用域变化 → `plan_invalidated`；
- 版本号未变但字段结构变化 → `form_structure_changed`（同样失效）；
- 计划引用的字段消失 → `planned_field_missing`；
- 门户必填项未被计划覆盖 → `portal_required_unmet`；
- 原文件不可用 → `planned_file_missing`。

任一阻断项成立时 `executable` 为 false，`fields` 和 `csrf` 为空，避免“带病提交”。
只有显式调用 `drill.submit(...)` 才会访问本地演练门户。

## 8. 版本变化的影响报告

`analyse_impact(before, after, rules, plans, drafts, case_decisions, ledger, files)`
比较两个表单版本，输出：

- `diff`：`removed` / `added` / `split` / `merged` / `renamed` / `retyped` /
  `required_changed`；
- `impacts`：每个变化字段关联的规则、计划、草稿；
- `affected_rules`：旧规则保持原样并标记“未自动迁移”，附完整事件历史；
- `affected_plans`：已确认计划标记需要重新生成并再次确认；
- `affected_drafts`：草稿逐项标为 `needs_review` / `unaffected`，**不自动迁移**；
- `preserved`：原文件、项目内例外、裁定事件、规则历史、计划与草稿快照。

拆分与合并只认表单声明的 `split_of` / `merged_from`；没有声明的改名会表现为
“删除 + 新增”，不会被猜成同一字段。

## 9. 本地演练

```sh
python3 -m unittest discover -s tests -v
python3 -m permit_forms drill --application APP-WEST-001 --after "west|lab|2026-c"
```

`drill` 依次输出：

1. `reviews`：四笔申请各自的映射审阅（含证据不足与未套用的历史决定）；
2. `plan`：选定申请的计划，含阻断项；
3. `confirmation`：确认结果，或计划被阻断的原因；
4. `preflight`：用目标版本检查计划，明确是否失效；
5. `impact`：两个版本之间的字段变化、受影响规则、计划与草稿，以及保留的历史。

也可单独使用：

```sh
python3 -m permit_forms review --application APP-WEST-001
python3 -m permit_forms plan --application APP-WEST-001
python3 -m permit_forms preflight --application APP-WEST-001 --current-form "west|lab|2026-c"
python3 -m permit_forms impact --before "west|lab|2026-b" --after "west|lab|2026-c"
python3 -m permit_forms ledger
python3 -m permit_forms ledger --explain CASE-WEST-001-A
```

`--snapshot` 可指定 SQLite 文件保存裁定记录；重复运行不会重复写入。

## 10. 边界

- 不调用任何真实门户，不安装依赖，不联网；
- 不做模糊匹配、相似度打分或材料类型猜测；
- 不自动把项目内例外推广为规则，也不自动迁移规则、计划或草稿；
- 门户必填项未满足时只输出计划与阻断原因，不提交。
