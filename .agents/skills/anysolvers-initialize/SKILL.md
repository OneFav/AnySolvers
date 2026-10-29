---
name: anysolvers-initialize
description: "初始化 AnySolvers 工作环境：交互明确问题、数据和算力，准备并封存 task、datasets、ideas。用于建立或复用 study，不启动研究循环。"
---

# AnySolvers 初始化

通过当前主会话完成初始化，让用户从问题描述得到可复用的研究环境。此 Skill 是八模块的入口，不另建一套模块逻辑，不委派研究员。

## 定位与读取

项目根目录是此文件向上三级、含 `tools/run.py` 和 `modules/` 的目录。先读 [AGENTS.md](../../../AGENTS.md)、[README.md](../../../README.md) 和用户选定的配置。以下路径均相对项目根，命令从项目根执行并加 `.venv/bin/python -B`。

从配置的 `modules.<类别>.use` 读取实际 task、datasets、ideas 的 `MODULE.md`；按需读其 schema/handler。默认参考 [task](../../../modules/task/problem_spec/MODULE.md)、[datasets](../../../modules/datasets/stratified_holdout/MODULE.md)、[ideas](../../../modules/ideas/diverse_hypotheses/MODULE.md)。替换了方法就遵守新包，不能仍套用默认实现。CLI 参数不确定时查 `tools/run.py --help`，不要猜命令。

## 1. 通过交互补齐信息

先提取本轮请求、已有文件和已确认的答案。只问缺项，每次一至三个简短问题，给出有依据的推荐；不要让用户填写全部技术字段。用户已明确的需求和授权不重复询问，可并行准备不依赖答案的部分。

| 要明确的内容 | 足以落地的答案 |
| --- | --- |
| 问题 | 决策对象、算法输入/输出、硬约束、成功条件；静态求解还是逐步交互 |
| 指标 | 主指标公式、单位、优化方向、聚合方式；合法性与失败如何单列，必要的辅助指标 |
| 数据/环境 | 指定数据或仿真器的路径、来源版本与许可；或允许构造哪些合成实例；可见状态与隐藏未来信息 |
| 覆盖范围 | 与用户明确规模、难度、场景分层、开发集与独立最终集各自的数量/划分；官方划分和随机种子要求，不默认固定容量 |
| 资源 | 本地/远端、可用运行环境、CPU/内存和用户的计算约束；单案及整次评测时限由初始化根据任务提出默认建议并告知用户；交互任务另明确动作时限和终止规则 |
| 初始想法 | 需要多少个有区别的起点、已有方法/基线与禁止的捷径；未知时提出小规模建议 |

建模和实现细节由主会话完成，不要求用户写评分器。无法推断的目标、数据授权、关键现实约束要先问；可逆的实现选择自行完成并记入 task.md。研究人数、轮数和总费用尚未确定时留到第二个 Skill，不能把准备阶段建议当作执行授权。

当前默认后端是本地 Docker、三个评测槽位、每个案例单核、无网络/GPU，支持静态 `solve(case)` 和轻量交互 `solve(observation)`。检查实际余量和其他任务占用，不能按三个槽位就假定资源足够。如果需求是远端执行、训练或大型仿真，先说明适配缺口，不把配置填写当成已实现。不要擅自启用其他项目的资源池。

## 2. 准备 study 与基础配置

为新问题选择唯一 STUDY。相同初始化供多个编号批次复用；已有 study 先运行 `tools/run.py study-check STUDY`，检查 status，而不只看 ok。sealed 且匹配需求就直接复用；已登记的 draft 继续补齐未交付部分，不重复 study-init。不同问题、数据划分或评分规则创建新 study，不覆盖已封存资产。

新建 `configs/STUDY.yaml`（schema_version 5）作为可复用基础配置；可参考 [smoke.yaml](../../../configs/smoke.yaml)，但要替换样例任务、镜像与不适用的容量。配置的 task 指向 `runs/STUDY/initialization/task/task.json`。保留默认研究员 `gpt-6.1-sol/high`，除非用户另有指定；基础配置不支持再次 extends。

初始化必须有合法配置，但其中人数/轮数/时限是准备时的默认值，启动前还要明确本批有效值。`max_cases_per_dataset` 默认 null、无固定容量上限；这不代替与用户确认各集合实际数量，也不允许空集合。用户显式设整数上限时必须容纳各集合，包含 final，不能截断真实数据迁就样例。仅准备环境不授予最终测量权限，`final.authorized` 应与用户实际授权一致，不能照抄样例的 true。

先只准备 `runs/STUDY/initialization/task/task.json` 和 `task.md`：前者是结构化任务定义，后者含建模、指标公式、约束、观测边界及假设。然后执行：

```sh
.venv/bin/python -B tools/run.py study-init STUDY --config configs/STUDY.yaml
```

原地准备时，此刻 study 只能包含 `initialization/task/` 中的这两文件；不可提前创建 datasets、ideas 或编号批次，否则初始化会拒绝。`study-init` 登记任务，不会自动编写仿真器或算法。

## 3. 建立可执行评测环境与初始 ideas

由 datasets 包完成，具体字段读所选模块的 schema，不复制另一份接口定义：

- 在 `initialization/datasets/` 准备共享 `benchmark.json`、可信评分器/仿真适配器、基线代码、规则 fixtures、必要支持文件，以及各集合的 manifest 和实例。环境只存一份，各划分引用；task 目录不放执行程序。
- 指定真实数据时使用获授权的真实文件和转换器，保留来源与官方划分。数据缺失就说明缺项，不用合成数据替代。需要联网获取数据时先确认请求范围与访问条件，不用无来源的生成内容充当实测数据。
- 仿真任务明确动力学、资源/碰撞约束、任务释放、终止和计分。策略只获得允许的 observation；为隐藏未来事件、物料守恒或其他关键约束做针对性检查。不要宣称已有宿主级文件隔离。
- 审阅将被宿主执行的可信环境/转换器；检查有效实例、手算分数、非法输入、非法解/动作、划分重叠和转换一致性。初始化登记会执行这些可信规则检查，不会执行候选或基线。
- 主会话根据问题、开发信息和必要的一手资料生成 N 个机制不同且可检验的 ideas，含假设、机制、首次改动、风险和来源；不凭最终集表现筛选 ideas。

逐个准备并登记，路径相对 `initialization/`，不是 manifest 所在目录。下面 DATASET_ID/IDEAS_ID 应替换为实际标识：

```sh
.venv/bin/python -B tools/run.py study-deliver STUDY dataset datasets/DATASET_ID/manifest.json --module datasets
.venv/bin/python -B tools/run.py study-deliver STUDY ideas ideas/IDEAS_ID.json --module ideas
.venv/bin/python -B tools/plan_resources.py STUDY --config configs/STUDY.yaml
```

资源建议按当前协议、集合数量和评测实现的启动/清理预留量匹配单案预算与整次评测预算。命令不运行代码、不改配置，不解析 final 输入；没有实测时输出明确标为启发式起点。主会话按任务复杂度与公开基线证据选择合适值，告知用户单案墙钟/CPU/内存、交互响应时限、整次评测时限和三槽的主机需求，将最终值写回 `configs/STUDY.yaml`，而不要求用户自行估计全部技术参数。若已有获授权的公开基线测量，可用 `--observed-wall-s`、`--observed-cpu-s`、`--observed-memory-mb` 加 `--measurement-note` 输入；该工具不会核验或重跑这些测量。需要新实测时先确认有界验证授权，再通过沙箱计账；不能直接在宿主运行基线。

单案预算不随并发槽数缩减。评测次数默认不限，但每次评测和单案计算仍有时限；排队与评测耗时不消耗研究员的 40 分钟有效研究时间。数量或预算确定后再封存：

```sh
.venv/bin/python -B tools/run.py study-seal STUDY
.venv/bin/python -B tools/run.py study-check STUDY
```

研究前准备好独立 final 集并封存，后续启动最终测试才能复用；最终输入不进入研究员材料。若用户明确不需要 final，说明此 study 不能直接启动需要 final 的批次，不事后根据研究结果补造测试集。

## 4. 交接与停止点

交付 `configs/STUDY.yaml`、`runs/STUDY/initialization/manifest.json` 及其 task/datasets/ideas 资产。原始问答、公共 Skill、模块说明不复制到 runs；已确认的建模决策进入 task.md，配置进入 YAML，数据来源进入数据 manifest。

简要告诉用户：study 名称、任务/主指标、集合与来源、实际通过的检查、剩余限制、下一步需明确的运行预算，并链接配置和共享初始化。严格区分“规则与结构检查通过”和“基线性能已实测”。

此入口默认不创建编号研究批次、不调用模型、不执行候选或基线。基线沙箱预检由研究启动入口在预算内完成；若用户另要求准备阶段实测，先明确有界验证预算，再按同一批次/沙箱接口计账。用户只要求初始化时在此结束；已明确要求连同研究一起启动时可交接 `$anysolvers-research`，沿用已有答案和授权。
