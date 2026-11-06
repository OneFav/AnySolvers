---
id: frozen-holdout
version: 2.0.0
kind: final
accepts:
- run_config
- task
- dataset
- candidate
- evaluation
- selection
- authorized_inputs
produces:
- report
execution:
  mode: main
  delegates: {}
contracts:
  report: schemas/report.json
handlers:
  select: policy.py
  recommendation: policy.py
  gate: policy.py
  validate_delivery: validate.py
files: []
defaults: {}
capabilities: {}
---

# 最终门禁与报告

负责终局候选冻结、最终评测准入和报告，最终证据在此结束研究反馈。只读取已授权证据，不产生新的研究候选或将 final 表现送回研究。

## 输入与文件

输入为固定任务、开发评测、冻结选择、已封存 final 数据、最终评测授权及可用测量。输出 `runs/RUN_ID/final/REPORT_ID.md`（或同等报告路径）和 manifest：`schema_version: 1`、`report_id`、`report_file`、`evaluation_ids`。evaluation_ids 只能引用本次已结束测量；可为空，以说明测量前中止。

## 方法

1. research 决定结束研究并确认没有活动研究或测量后，`select` 仅依据完整合法、非 probe 的 anchor 结果，按任务目标方向及固定同分规则冻结最优候选与源码。ranking 仅用于此终局冻结，不生成算法想法、不决定中途迭代或 actor 调度。冻结后停止新增研究资产。
2. `gate` 校验 `final.authorized`、选择冻结、final 交付时间/封存及所选源码哈希。未满足时说明缺失条件，不能启动最终测量。
3. 有授权且门禁满足时，主对话通过 evaluation 仅测量基线与冻结候选，最终测量写入本批 `final/evaluations/`。探测、修改、改选或按 final 调整协议不属于本次运行后续动作。
4. 报告任务定义、数据来源与用途、候选改动、预算、合法性、分数、失败和基线比较，引用实际资产路径及评测 ID。分别呈现开发结果与最终结果。
5. 区分实测结论与待验证预测，解释样本覆盖、探测筛选偏倚、基础设施失败及未完成事项。最优性、普遍提升或理论反例均需对应证据。
6. 报告完成后可交 visualization 将授权结果绘图；图表不增加测量，也不能反馈调整本次研究。后续修改另行授权并建立独立运行。

## 调用与完成条件

`tools/run.py select RUN_ID --candidate CANDIDATE_ID` 调用本模块 `policy.py` 的 select handler；完成冻结后，evaluation 的 final 请求调用 `policy.py` gate；主对话据门禁结果安排获授权测量。报告以 `deliver RUN_ID report final/REPORT_ID.json --module final` 登记，由 `validate.py` 检查引用。

成功返回 report manifest、已有结果和限制。未授权最终评测或研究提前终止时仍报告已有研究结果与缺失步骤，不将研究分数标为独立最终成绩。测量失败时如实报告原因，不修改候选重跑 final。


执行与授权、路径、冻结交付、原生参数及预算边界遵守主 [AGENTS.md](../../../AGENTS.md)；具体交付或程序调用以上述本模块规则为准。

`tools/run.py recommendation RUN` 只读返回按正式 select 相同规则得到的候选、源码哈希、开发评测 ID 和分数；没有合格候选时返回 null。它不冻结候选、不授予最终评测权限，select 时仍重新核验。若 selections 启用每轮方法报告，终局冻结前必须完成所有已结束会话的报告。
