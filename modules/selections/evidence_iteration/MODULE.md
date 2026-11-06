---
version: 3.0.0
execution:
  mode: main
  delegates: {}
defaults: {}
capabilities:
  researcher_authored: true
  method_reports: true
id: evidence-iteration
kind: selections
accepts:
- candidate
- evaluation
- ideas
- iteration
produces:
- iteration
contracts:
  iteration: schemas/iteration.json
  method_report: schemas/method_report.json
handlers:
  context: policy.py
  validate_delivery: policy.py
  evidence: policy.py
  report_context: reports.py
  report_submit: reports.py
  report_audit: reports.py
  pending_reports: reports.py
  require_reports: reports.py
files:
- evidence.py
- reports.py
---

# 下一代算法迭代想法

在 research 会话中由研究员基于开发反馈提出算法改动，直接调用 tools/research.py iteration 登记；不启动 selection agent，主会话不代写下一步算法。

iteration schema 包含 author_session、parent_candidate、source_idea_ids、evidence_ids、history_iteration_ids、observations、hypothesis、algorithm_changes、expected_effect、test_plan、risks、rationale。必须描述具体机制和可验证预测，不是人员/预算/下一轮调度计划。

程序验证作者会话仍active、证据属于已封存开发测量、私有历史归属和父候选来源。公开发现的证据可引用，最终结果禁止进入。context 只返回本人的候选/迭代和明确公开的成绩。

研究员随后 checkpoint 将 next_iteration_id 指向此想法。主会话只按该引用安排下一原生会话，后者读取笔记继续。终局按分数选择固定候选属于 final，不是本模块职责。

## 每轮方法报告

每轮结束（含最后一轮）由主会话核对实际封存源码并写方法报告，不改变研究员对下一轮 iteration 的创作权。`checkpoint.candidate_id` 指定本轮最终方法，可以保留本人早先轮次的候选；null 表示没有封存方法。不得自动用最高分或最后提交的候选补位。正常结束必须有 checkpoint；失败会话没有 checkpoint 时记为无方法。

主会话使用以下管理接口，研究员的会话接口不开放这些操作：

```sh
python tools/run.py method-reports STUDY/001
python tools/run.py method-report-context STUDY/001 --session r1-r001
python tools/run.py method-report-submit STUDY/001 --session r1-r001 --manifest research/inbox/report-r1-r001.json
```

context 返回该方法源码、可信开发事实及 schema。正文为 `{"candidate":{"candidate_id":"...","title":"...","method":"...","limitations":"..."}}`；无方法时为 `{"candidate":null}`。title/method/limitations 分别最多 40/280/80 字符。用普通算法术语独立说明输入、步骤、分支、并列选择、终止条件和输出，不用“沿用基线”代替说明，不把笔记中的计划写成已实现功能。正文不填写分数，程序附上该候选所有已结束开发评测，包括失败、probe、合法性、覆盖和来源哈希；未测量明确标注。最终成绩不进入这些报告。

报告封存在 `research/reports/<session>.json`，绑定本轮起点、最后 checkpoint、候选源码和开发证据，不可覆盖。本人下一轮准入和终局选择都检查报告完整性；其他研究员继续独立推进。程序检查身份和证据一致性，主会话负责方法文字是否忠于源码。
