# 材料映射与填报计划

本文档说明 `mapping.py` 与 `forms.py` 在原始接收（`archive.py` 等）之上提供的业务层：
按范围生效的材料映射、两类人工裁定、可解释的字段建议、填报计划与版本变化衔接。
全部推导基于精确匹配与人工裁定，不使用相似词猜测。

## 范围键

映射的生效范围是三元组 `(district, application_type, form_version)`，即地区、申报类别、
表单版本。同一份材料在不同范围可以对应不同栏目：甲区 2026-a 把行业证明归为
`business_licence` 一栏，乙区 2026-b 要求拆到 `industry_a`、`industry_b` 两栏。跨范围的
规则一律不自动适用；表单版本变化后，旧版本的规则不会延续到新版本。

## 人工裁定的两种类型

`record_ruling` 只接受两种 `scope_type`：

- `project`（项目内例外）：必须带 `project_id`，只影响该项目的建议。其他项目、其他
  版本的推导不会引用它，一笔特殊处理不会被推广给所有申请。
- `reusable`（可复用规则）：不绑定项目，对同范围所有项目生效，并生成一条 `active`
  规则。同范围同材料类型已有生效规则时，旧规则标记 `superseded`（保留可查），新规则
  取而代之。

两表均只追加、不删除；裁定必须填写理由与裁定人，作为建议解释的来源。

## 字段建议：可解释与证据不足

`suggest(project_id, form)` 每次由裁定与规则实时推导，因此表单刷新不会覆盖人工确认。
每个字段的结果为二选一：

- `suggested`：给出建议文件及 `basis`——依据类型（`project_ruling` 或
  `reusable_rule`）、裁定/规则编号、材料类型、理由、裁定人、生效范围；同类型多份
  材料时列出备选。
- `insufficient_evidence`：明确列出已检查项（本范围无生效规则 / 规则要求的材料类型
  项目未登记 / 本项目无例外）。被取代或受版本影响的旧规则只作为
  `historical_rule` 背景展示，注明“不作为建议依据”，绝不参与推导。

项目内例外优先于可复用规则；例外要求的材料类型未登记时，该字段为证据不足，
不会悄悄回落到可复用规则。

## 表单快照与计划锚定

`forms.py` 把一次门户观察规范化为 `FormSnapshot`：字段集合（名称、标签、是否必填）、
表单版本、csrf 令牌，并对规范化内容计算 SHA-256 摘要（字段顺序不影响摘要）。
快照来自门户页面的 `source-records` 数据块与表单隐藏控件，缺一即报错。

填报计划生命周期：

```
build_plan     必填未满足 → blocked（输出供审阅，不可确认、不提交）
               必填已满足 → draft
confirm_plan   draft → confirmed（记录确认人；blocked/stale/executed 拒绝）
execute_plan   仅 confirmed 可执行；执行前重新观察表单，
               摘要一致 → 逐栏提交并记录回执（executed）
               摘要不一致 → 标记 stale 并拒绝，不发出任何提交
```

计划锚定确认时的快照摘要与 csrf。门户升级轮换令牌时，即使强制提交也会被门户
以 409 拒绝，形成双重防线。任何环节都不会自动提交。

## 版本变化的影响标记

`register_snapshot` 登记每次表单观察。与上次观察（同地区同类别）摘要一致时不产生
影响；不一致时返回影响报告并写入 `form_changed` 事件：

- 字段对比：`removed_fields` / `added_fields` / `kept_fields`，标签完全一致的删增对
  列为 `possible_renames`（仅提示，不自动迁移）；
- 旧版本全部 `active` 规则标记 `impacted`，规则行保留，含原目标栏目；
- 绑定旧摘要的未执行计划（blocked/draft/confirmed）标记 `stale`，计划行保留原
  分配，可回查“当时准备怎么填”。

新版本的字段在人工重新裁定前保持证据不足；重新裁定生成新范围的规则后，建议与
计划恢复正常。

## 可追溯性

- 原始页面与项目文件原始字节由 `Archive` 按摘要保存（`portal-form`、
  `project-document` 来源），`documents.meta.capture_id` 关联到具体抓取；
- `events` 表只追加地记录材料登记、裁定、计划各状态迁移、快照登记与版本变化；
- 规则与计划的全部历史状态（active/impacted/superseded、blocked/draft/confirmed/
  stale/executed）保留在各自表中，不按最新状态覆盖。

## 本地演练

```sh
python3 -m permit_forms drill
```

演练场景见 `fixtures/drill.json`（2026-b 当前表单与 2026-c 升级内容均逐项声明）。
`drill.py` 从空临时目录启动本地门户，依次演示：初始证据不足 → 乙区可复用裁定 →
刷新不覆盖裁定 → 项目内例外不外溢 → 计划确认并执行 → 门户升级拆分字段 → 影响
报告 → 已确认计划拒绝执行（且门户 409）→ 新版本重新裁定 → 新计划执行 → 可追溯
清单。输出为按步骤组织的 JSON，每步带中文摘要。
