---
id: stratified-holdout
version: 3.0.0
kind: datasets
accepts:
- run_config
- task
- authorized_inputs
- dataset
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
defaults:
  count: null
  seed: 7
capabilities:
  dataset_purposes:
  - anchor
  - challenge
  - final
  candidate_feedback: false
parameters: schemas/params.json
---

# 评测环境与分层实例初始化

由主会话完成本模块：根据 task 的问题定义，接入现成真实数据，或构建合成/仿真场景，同时准备可信评测接口、基线与规则检查。登记程序只检查已准备的本地资产；它不会自行生成仿真器、下载数据或以合成数据替代缺失真实数据。研究阶段的运行与计分由 evaluation 完成。

## 输入与产物

输入是固定问题定义、用户指定数据/环境及使用许可、算力与预算、分层或官方划分要求。`count` 默认 null，表示尚未决定数量；初始化时必须与用户明确每个集合的实际数量和分层，再准备实例。`seed` 是自建实例的准备参数；指定真实数据时尊重原始记录和官方划分，不以随机合成凑足数量。`budgets.max_cases_per_dataset: null` 表示没有框架容量上限，不表示允许空集合或自动决定数量；显式设置整数时仍检查该上限。共享初始化保存当时的容量规则，本批新增实例使用本批规则。

共享资产位于 `runs/STUDY/initialization/datasets/`，例如：

```
datasets/
  environment/
    benchmark.json
    evaluate.py          # 静态评分器，或可信仿真环境适配器
    baseline.py          # solve(case)，或 solve(observation)
    fixtures.json
    ...                  # 明确登记的地图、模型、支持代码等
  raw/...                # 获授权的真实原始文件，可由多个划分共用
  anchor/{manifest.json,cases.jsonl,adapter.py}
  final/{manifest.json,cases.jsonl,adapter.py}
```

每个数据集 manifest 指向同一个 benchmark，环境不逐实例或逐划分复制。每行实例仍为 `{"case_id":"case-a","input":{...}}`；input 可以是静态数据，也可以是地图、初始状态、物理参数、任务过程种子和仿真时长。大文件通过已登记的共享支持文件读取。候选能看到哪些信息由协议决定，不能把隐藏未来事件当成普通公开输入发给策略。

本批追加 challenge 放在 `runs/STUDY/NNN/research/datasets/`；`benchmark_file: "@anchor"` 引用该批次已封存的共享 anchor 环境，避免复制环境。这个引用仅用于本批 challenge，不能用于共享初始化或 final；原始数据和转换器仍登记在本批目录。

## 数据集 manifest

字段：`schema_version: 1`、`dataset_id`、`task_id`、`purpose`（anchor/challenge/final）、`cases_file`、`benchmark_file`、`generator`、`source`、`parents`。文件路径相对于本次交付根目录（initialization 或编号批次），不是相对于 manifest。

- `generator` 为 `{plugin_id,code_file,seed,parameters}`；plugin_id 匹配本模块 id，代码/种子可为 null。真实导入在 parameters 中说明划分和转换步骤。
- `source` 必含 `{kind,reference,version,description,license,files,adapter_file}`。kind 为 synthetic / real / simulation；reference 写数据集名称或原始来源，version 写固定版本，license 写许可依据，files 是原始本地文件路径列表，adapter_file 可为 null。
- **real 必须具有非空原始文件列表和转换器**。转换器提供 `load_cases(raw_paths) -> list[case_row]`，顺序和内容必须与提交 cases.jsonl 完全一致，登记时实际核验。这支持真实官方划分及字段转换，拒绝“声明真实但实际另造实例”。转换器应忠实转换，不生成替代记录；程序检查字节关联和转换一致性，不能独立证明提供方真实性或许可有效性。
- 本地数据未取得、许可不明、转换失败时停止登记并说明缺项，不自动联网、不回退合成。simulation 可引用已安装环境；其依赖版本和资源需求须写入问题说明和支持资产，当前执行环境必须已具备这些依赖。

## 可执行评测接口

`benchmark.json` 包含 `schema_version:1,benchmark_id,task_id,protocol,evaluator_file,baseline_file,tests_file,support_files`。support_files 是全部额外支持资产的显式列表。全部路径相对于交付根；评分、基线、测试、支持文件与原始数据均参与封存校验。同一研究各划分必须使用同一环境签名。可信环境代码可通过 `__file__` 定位登记资产；不得依赖未登记的本地可变文件。

静态协议 `protocol: {kind: function}`：

```python
validate_input(case)                  # 不合法时抛出 ValueError
score(solution, case)                 # -> {score: finite_number, metrics: {}}
# 候选及基线：solve(case) -> JSON 解
```

交互协议 `protocol: {kind: episode, max_steps: 100, action_wall_s: 1}`：

```python
validate_input(case)
reset(case)                          # -> (private_state, observation)
step(private_state, action)          # -> (private_state, observation, done)
score_episode(private_state, trace, case)  # -> {score, metrics}
# 候选及基线：solve(observation) -> JSON 动作
```

评分结果可另含 trace 详情。observation 必须是 JSON 对象，done 必须是 bool。private_state 仅在可信环境中存在；策略进程只收到 observation。环境负责动力学、碰撞/资源约束、任务释放、终止和评分，算法不能自行决定这些规则。trace 的每步记录为 `{step,observation,action,next_observation,done}`，step 从 0 开始。响应时限与单案总预算共同生效；max_steps 是终止保护，不额外扩展计算预算。

本版本支持固定策略的交互评测，不自动承诺强化学习训练、GPU训练或额外依赖安装。需要训练时，应先明确训练/测试分离与预算，不能将训练隐藏进可信评分或擅自增加运行阶段。

## 检查、交付与完成

1. 明确静态/交互协议、观测边界、原始来源、分层与独立最终集。源数据有官方划分时优先保留其语义；默认生成方法不得依据候选成绩挑选 final。
2. 编写可信环境与基线。fixtures.json 包含 `schema_version:1,valid,invalid_inputs`；静态另外含非空 invalid_solutions，交互另外含非空 invalid_actions。静态 valid 项为 `{case,solution,expected}`，交互为 `{case,actions,expected}`，expected 为精确 `{score,metrics}`（可另含 trace 详情）。交互有效序列必须自然结束；非法动作序列应由环境拒绝。至少提供一个有效、一个非法输入及一个非法解/动作检查。检查项可带 name；非法输入可写为 `{name,case,error_contains}`，非法解/动作可带 error_contains 核验具体错误。
3. 实际检查输入合法性、ID与输入内容唯一、原始数据转换一致、fixture结果、案例容量、final/开发输入无重叠。交互任务另外提供隐藏未来事件隔离检查和物理/计分 sanity checks；通用回调检查本身不能证明整个模型的现实性。
4. 共享准备调用 `study-deliver STUDY dataset datasets/DATASET_ID/manifest.json --module datasets`。本批新增 challenge 调用 `deliver STUDY/NNN dataset research/datasets/DATASET_ID/manifest.json --module datasets`。程序不执行候选或基线；基线性能检查通过 evaluation 沙箱计入预算。
5. 登记各集合后、封存前调用 `tools/plan_resources.py STUDY --config configs/STUDY.yaml`。本模块的 `recommend_compute` 根据静态/交互协议给单案时间、CPU、内存起点，并按最大集合数量与 evaluation 提供的每案启动/清理预留量计算整次评测时限。默认三个槽位；需同时检查本机余量及其他任务。该命令只生成配置建议，不执行算法、不写配置，也不会解析最终实例内容。
6. 主会话结合任务复杂度和已获授权的公开基线实测决定最终预算，向用户说明并写回基础配置，再封存。可传 `--observed-wall-s`、`--observed-cpu-s`、`--observed-memory-mb` 与 `--measurement-note` 提供实测值；程序只按裕量计算，不独立核验其真实性。未实测时明确为启发式起点，不能声称已校准。交互的动作时限/步数仍由协议单独定义；单案时限不按槽数缩减，排队不计入整次评测时限。后续新批次若引入更大集合，重新匹配整次时限。
7. 交付完成后，后续 research/evaluation 只引用这套环境和实例。默认摘要返回分数、合法率与文件引用，具体轨迹按需读取。

执行与授权、路径、封存、预算边界遵守主 [AGENTS.md](../../../AGENTS.md)。该模块不启动模型或正式研究，不自行采集外部数据。
