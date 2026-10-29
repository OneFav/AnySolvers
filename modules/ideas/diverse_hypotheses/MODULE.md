---
id: diverse-hypotheses
version: 2.0.0
kind: ideas
accepts:
- run_config
- task
- authorized_inputs
- dataset
- candidate
- evaluation
produces:
- ideas
execution:
  mode: main
  delegates: {}
contracts:
  ideas: schemas/ideas.json
handlers: {}
files: []
defaults: {}
capabilities: {}
---

# 可检验研究想法

从授权资料或开发评测证据提出能够由一次具体改动检验的假设。本模块只分析资料，不执行候选。

## 输入与文件

输入为固定任务、授权开发数据/候选/评测、已有观察及剩余预算。共享初始想法在 `runs/STUDY/initialization/ideas/IDEAS_ID.json` 准备并封存，批次引用原文件。研究中新增想法在 `runs/RUN_ID/research/ideas/IDEAS_ID.json` 输出，不覆盖共享资料。manifest：`{"schema_version": 1, "ideas": [...]}`，列表至少一项。登记 ID 取 manifest 文件名，故文件名也必须符合 ID 规则。

每条想法包含 `idea_id`、`hypothesis`、`mechanism`、`first_change`、`risks`、`source_refs`。前五项为非空字符串，source_refs 为字符串数组，可为空；只引用实际读过的资料路径、想法或评测 ID。

## 方法

1. 从已观察机制或失败出发，区分观察、推断与待验证预测，写出可证伪假设。
2. 写清作用机制、首个实现改动、预期效果、失效条件和风险；未测量效果标为预测。
3. 对比实现与测量成本，优先选择可在剩余预算内区分原因的改动；列出所需资料、依赖与测量。
4. 给主对话提交推荐假设与可推翻条件，由 research 主循环安排后续实现或额外数据，selections 只提出下一次具体迭代；不读取 final 结果来生成本次研究假设。

## 调用与完成条件

共享初始想法通过 `study-deliver STUDY ideas ideas/IDEAS_ID.json --module ideas` 登记；本批新想法通过 `deliver RUN_ID ideas research/ideas/IDEAS_ID.json --module ideas` 登记。成功交付结构有效、来源可追溯的想法包及推荐理由；资料不足时说明缺口，不把无证据的效果宣称为提升。此模块无程序 handler，通用交付入口按本地 schema 校验。


执行与授权、路径、冻结交付、原生参数及预算边界遵守主 [AGENTS.md](../../../AGENTS.md)；具体交付或程序调用以上述本模块规则为准。
