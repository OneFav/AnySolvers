---
id: feedback-challenges
version: 3.0.0
kind: datasets
accepts:
- run_config
- task
- authorized_inputs
- dataset
- candidate
- evaluation
produces:
- dataset
execution:
  mode: main
  delegates: {}
contracts:
  dataset: schemas/dataset.json
  case_row: schemas/case_row.json
  benchmark: schemas/benchmark.json
  fixtures: schemas/fixtures.json
handlers:
  validate_delivery: package.py
  inventory: package.py
  load_environment: package.py
  recommend_compute: resources.py
files: []
defaults: {}
capabilities:
  dataset_purposes:
  - challenge
  candidate_feedback: true
---

# 反馈驱动的困难实例

这是 datasets 槽位的可替换完整方法包，保持 dataset/case_row/benchmark/fixtures 接口，只支持 challenge，允许候选反馈。先将完整目录从备用区移到 `modules/datasets/feedback_challenges/`，再绑定该路径；方法中的相对引用以本 Markdown 所在目录为基准。

## 输入、输出与步骤

1. 读取授权任务、开发候选、父开发集与已有开发评测，写清待检验机制或可证伪主张及失败条件。
2. 在任务合法域内构造实例；输出本批 `research/datasets/DATASET_ID/cases.jsonl`、manifest 及生成代码；不写共享 initialization。沿用 dataset 契约全部字段；`benchmark_file: "@anchor"` 复用共享初始化中封存的环境，不复制或修改仿真器与评分规则；`purpose` 必须为 `challenge`，`generator.plugin_id` 匹配 `feedback-challenges`。
3. 需候选反馈时先交付合法探测数据集及候选，由研究员直接使用 evaluate --probe（或主会话在获授权的初始化检查中调用 evaluation）测量；此操作同时消耗评测与探测预算。
4. 用新 ID 保存可重放的失败实例，在 generator.parameters 记录构造/筛选机制、参数、探测评测 ID 和选择偏倚，parents 引用已交付数据。
5. 只有证据证明违反明确主张时才称为反例，其余表述为困难实例及具体比较结果。筛选结果不作为独立 final，不修改 anchor 或已封存共享资料。

## 环境与来源

本包具有自己的契约和校验/加载程序，不导入兄弟方法实现。运行时由 load_environment 读取已登记的静态 function 或交互 episode 环境；环境回调由共享初始化提供，候选反馈只能影响新实例，不得改变规则、观测边界或评分。交互协议保持 `reset(case)->(state,observation)`、`step(state,action)->(state,observation,done)`、`score_episode(state,trace,case)`，候选为 `solve(observation)`。

每个 manifest 的 source 必含 kind/reference/version/description/license/files/adapter_file。合成构造、模拟场景和真实记录都可用于诊断，但必须如实区分。真实记录需同时登记原始文件与 `load_cases(raw_paths)` 转换器，转换输出必须与 cases.jsonl 完全一致；禁止没有原始数据时用合成数据冒充。按反馈筛选真实记录时，在转换器与 generator.parameters 中明确筛选步骤和偏倚，不能重新称为独立真实测试。

所有路径相对于批次根目录，全部原始文件、转换器、实例和生成程序在 research/datasets 内封存。共享环境仅通过 @anchor 引用；本包只创建 challenge，不接受共享 anchor/final，也不下载或安装环境。

## 调用与完成条件

启用目录后使用 `tools/run.py bind RUN_ID datasets modules/datasets/feedback_challenges`；核验新方法后由主对话直接执行，不委派子智能体。`deliver RUN_ID dataset ... --module datasets` 调用同一 validate_delivery handler。成功返回 manifest、检验假设、探测 ID 与证据限制；无可重放证据时不宣称反例。集合数量在初始化交互中明确；`max_cases_per_dataset: null` 不额外限制数量，集合仍必须非空，显式整数上限仍有效。`evaluation_calls: null` 不按普通评测次数限额；探测仍另受整数 `probe_calls` 约束，默认 0 不开放探测额度。单次/单案计算时限仍适用。

本包同时提供独立的 `recommend_compute` handler，依据现有协议、明确的集合数量及 evaluation 的资源契约给出计算预算建议。它不改评分规则、不执行算法；测量值若由调用者提供须来自已获授权的公开基线并标明来源。更大困难集可能需要更长的整次评测预算；冻结运行内不能偷偷扩时，应在新批次配置中确定并告知用户。


执行与授权、路径、冻结交付、原生参数及预算边界遵守主 [AGENTS.md](../../../AGENTS.md)；具体交付或程序调用以上述本模块规则为准。
