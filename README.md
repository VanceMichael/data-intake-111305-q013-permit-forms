# 行政申报附件清单的语义映射

本程序分两层：

1. **原始接收**：请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，
   能从 JSON、CSV 或门户嵌入的数据块读回来源记录。重复观察分别保留，原始内容按
   摘要复用。
2. **业务映射**：按地区、申报类别和表单版本，把材料文件建议到门户字段；每条建议
   都说明依据，证据不足时明说；人工裁定分为项目内例外与可复用规则；生成供申报员
   确认的填报计划；表单升级时标出受影响的旧映射与草稿。

业务层不生成自动提交，也不做行为推断。所有演示资料均为本地合成。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。运行过程
不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行
下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q permit_forms tests
python3 -m permit_forms demo
python3 -m permit_forms drill --application APP-WEST-001 --after "west|lab|2026-c"
```

`demo` 从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录。
`drill` 在本地合成资料上依次演示映射审阅、计划确认和版本变化衔接，**不提交任何
表单**。本地测试、编译和两个演示合计一般不超过十秒。

## 常驻来源与接收

```sh
python3 -m permit_forms serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m permit_forms capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及
完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供
协议演练。业务解释与原始接收相互独立。

## 业务映射

```sh
python3 -m permit_forms review --application APP-WEST-001
python3 -m permit_forms plan --application APP-WEST-001
python3 -m permit_forms plan --application APP-WEST-001 --confirm 申报员-甲
python3 -m permit_forms preflight --application APP-WEST-001 --current-form "west|lab|2026-c"
python3 -m permit_forms impact --before "west|lab|2026-b" --after "west|lab|2026-c"
python3 -m permit_forms ledger --explain CASE-WEST-001-A
```

要点：

- 规则、例外、字段声明都按 `地区 | 申报类别 | 表单版本` 精确生效；
- 建议附证据链与说明，证据不足返回原因和缺少的证据键；
- 项目内例外只对一笔申请有效，改用 `Ledger.promote_case` 才产生可复用规则；
- 表单刷新后与上次人工确认对比，有差异就要求重新确认，不静默覆盖；
- 计划在必填项未满足时为 `blocked`，不能确认；
- 确认后的计划在表单变化（含同版本号下的结构变化）时失效，不能继续执行；
- 版本升级报告标出被删除、拆分、合并、改名的字段及受影响的规则、计划和草稿，
  原文件与裁定历史保留可查。

详细设计与字段含义见 `docs/mapping.md`。

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `archive.py` / `client.py` / `decoding.py` / `origin.py` | 原始接收与本地来源 |
| `forms.py` | 表单版本模型、结构差异、来源记录聚合 |
| `mapping.py` | 作用域、材料文件、规则、项目内例外、映射审阅与解释 |
| `ledger.py` | 追加式裁定记录、规则提升、确认快照与历史 |
| `plan.py` | 填报计划、确认、执行前校验、门户载荷生成 |
| `impact.py` | 版本变化对规则、计划、草稿的影响 |
| `drill.py` / `rehearsal.py` | 本地演练编排与合成场景装载 |

## 开发资料

来源协议见 `docs/source-protocol.md`，映射与计划设计见 `docs/mapping.md`。领域观察
关系见 `tests/test_source_contract.py`；映射、记录、计划、影响与演练分别由
`tests/test_mapping.py`、`tests/test_ledger.py`、`tests/test_plan.py`、
`tests/test_impact.py`、`tests/test_drill.py` 覆盖。`fixtures/business_cases.json`
保存业务侧关注的交错情况，`fixtures/rehearsal.json` 是演练场景。所有条目、机构、
文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `permit_forms`、命名空间内的合成来源 JSON 和演练场景。
安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m permit_forms demo`
与 `python3 -m permit_forms drill`。提供 `serve --scenario` 时仍以指定文件为准，
找不到指定文件会直接报错，不回退到默认资料。
