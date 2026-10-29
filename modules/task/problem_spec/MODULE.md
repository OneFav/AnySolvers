---
id: problem-spec
version: 3.0.0
kind: task
accepts:
- run_config
- authorized_inputs
- task
produces:
- task
execution:
  mode: main
  delegates: {}
contracts:
  task: schemas/task.json
handlers:
  prepare: package.py
files: []
defaults: {}
capabilities: {}
---

# 问题定义

由主会话将用户需求整理为清晰的问题定义。本模块只回答“解决什么问题、哪些条件必须满足、怎样衡量效果”。仿真环境、实例、可执行评分器、基线和评测接口由 datasets 初始化；evaluation 只在运行时调用它们。

## 输入与产物

输入为用户目标、约束、授权资料、已指定的数据集/仿真环境、可用计算资源与预算。先区分用户已经确定的条件和待确认的假设；用户指定真实数据时保留这一要求，不能自行改成合成数据。

输出仅两份文件，优先位于 `runs/STUDY/initialization/task/`：

- `task.md`：数学定义、输入与输出含义、合法解/动作、约束、目标及单位、失败含义、观测和未来信息边界、适用范围。
- `task.json`：`schema_version: 2`、`task_id`、`version`、`description_file`、`objective`。objective 包含 name、direction（minimize/maximize）、aggregation（mean_if_all_valid）。此处不引用可执行程序，不固定 solve/step 接口。

## 定义方法

1. 明确任务是静态求解、时序交互，还是含训练的策略评估。交互任务写清决策时点、可见信息、动作、状态变化、终止与目标；完全可观测当前状态不等于可读取未来事件。
2. 明确实例分布或指定数据的来源/官方划分要求、规模和应用条件；具体数据转换与仿真实现在 datasets 完成。现成环境优先通过适配器复用，不为接口统一而改变原始问题。
3. 写清主要指标与辅助指标、约束违规与基础设施错误的区别。聚合规则仍为全体合法才有全体均值；不以失败罚分替代合法率。
4. 涉及强化学习时分别写清训练、验证、最终评测的授权和预算。当前轻量执行器支持函数求解与固定策略交互评测；未具备训练接口时标明缺项，不隐含承诺训练支持。
5. 主会话核对定义后将基础配置 task 指向 task.json，运行 study-init。datasets 之后根据此定义交付可执行评测包。可信代码的审阅与规则验证属于 datasets 的完成条件。

## 调用与完成条件

`.venv/bin/python -B tools/run.py launch-spec --config configs/smoke.yaml --module task` 解析输入参数；主会话按本说明执行，不委派子智能体。`study-init` 原地登记两份定义文件，或从指定来源复制一次，不自动生成问题或评测程序。

完成时返回定义路径、关键假设和材料缺口。`package.py` 只做结构校验与文件登记，不执行模型，不生成仿真器或评分器。初始化封存后批次复用问题定义；修改问题应建立新 study。

执行与授权、路径及预算遵守主 [AGENTS.md](../../../AGENTS.md)。
