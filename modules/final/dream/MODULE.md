---
id: dream
version: 1.0.0
kind: final
accepts: [run_config, task, dataset, candidate, evaluation, selection, authorized_inputs]
produces: [report]
execution:
  mode: main
  delegates: {dream: dream}
contracts: {report: schemas/report.json}
handlers:
  select: policy.py
  recommendation: policy.py
  gate: policy.py
  validate_delivery: policy.py
  dream_context: worker.py
  dream_start: worker.py
  dream_finish: worker.py
files: [WRITER.md]
defaults: {}
capabilities: {wiki_writer: true}
---

# 冻结评测与终局 Dream

主会话按完整合法、非 probe 的 anchor 测量冻结候选。获授权后测量最终集的基线和冻结候选，交付报告。无最终评测授权时，报告可说明已有开发结果和缺项。wiki 使用当前批次公开发现、基线及各轮已封存方法报告所引用的开发测量。最终集成绩只用于报告和可视化。

## 交付报告后

1. 交付 report 的响应包含 `next.operation=final-dream`。执行 `tools/run.py final-dream RUN`。中断续接时也从此命令查看进度；已完成同一版本材料时返回已有回执。
2. 状态为 ready 时按返回的 spawn_options **实际启动一位专用原生子 agent**，传入 RUN、材料和包内 WRITER.md。立即用 `tools/run.py start-final-dream RUN --native-id ACTUAL_ID` 登记平台返回的 native_id，等待该 agent 直接更新本地领域 wiki。新上下文只给开发材料，不转发最终报告、最终成绩或研究员私有笔记。状态为 active 时续接原 agent。确认旧进程已停止后，可用返回材料重新启动，并以 `--replaces OLD_ID` 绑定接替进程。
3. 子 agent 返回已发布主题页的相对路径后，主会话执行 `tools/run.py finish-final-dream RUN --native-id ACTUAL_ID --page topics/主题.md`。若没有任何可引用开发测量，子 agent 说明原因，主会话省略 --page 登记。命令核验当前批次引文后保存页面版本和实际 native_id。只有实际返回并核验后才汇报完成；进程中断时核对已写页面，按同一主题续接。
