---
version: 2.0.0
execution:
  mode: main
  delegates: {}
defaults: {}
capabilities:
  researcher_authored: true
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
handlers:
  context: policy.py
  validate_delivery: policy.py
  evidence: policy.py
files:
- evidence.py
---

# 下一代算法迭代想法

在 research 会话中由研究员基于开发反馈提出算法改动，直接调用 tools/research.py iteration 登记；不启动 selection agent，主会话不代写下一步算法。

iteration schema 包含 author_session、parent_candidate、source_idea_ids、evidence_ids、history_iteration_ids、observations、hypothesis、algorithm_changes、expected_effect、test_plan、risks、rationale。必须描述具体机制和可验证预测，不是人员/预算/下一轮调度计划。

程序验证作者会话仍active、证据属于已封存开发测量、私有历史归属和父候选来源。公开发现的证据可引用，最终结果禁止进入。context 只返回本人的候选/迭代和明确公开的成绩。

研究员随后 checkpoint 将 next_iteration_id 指向此想法。主会话只按该引用安排下一原生会话，后者读取笔记继续。终局按分数选择固定候选属于 final，不是本模块职责。
