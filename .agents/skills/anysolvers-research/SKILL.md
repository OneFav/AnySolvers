---
name: anysolvers-research
description: "启动或续接 AnySolvers 的研究循环与终局：复用已封存 study，明确批次预算、模型和算力配置，管理研究员并完成获授权的最终评测与图表。"
---

# AnySolvers 研究与最终评测

由当前主会话推进已授权的研究和终局。只通过 research 模块管理原生研究员，evaluation/selections 内嵌于研究员工作；主会话管理启动、预算、异常及最后检查，不逐次转发评测。此 Skill 不实现另一个后台循环。

## 定位与读取

项目根目录是此文件向上三级、含 `tools/run.py` 和 `modules/` 的目录。先读 [AGENTS.md](../../../AGENTS.md)、选定基础/批次配置及研究初始化 manifest。命令在项目根执行，加 `.venv/bin/python -B`。

运行 `tools/run.py study-check STUDY`，必须确认 status 为 sealed，而不只看 ok。按配置读取实际 research 的 `MODULE.md`，它是唯一循环说明；在对应阶段读取 evaluation、selections、final、visualization 方法包。当前默认 [research](../../../modules/research/notebook_collaboration/MODULE.md) 和 [final](../../../modules/final/frozen_holdout/MODULE.md) 仅作入口指引，换包后不能沿用旧方法规则。接口以 CLI `--help`、所选模块的 contracts/handlers 为准。

初始化缺失或不符合本次问题时先交接 `$anysolvers-initialize`，不能自行拿样例任务充当用户的问题。若是续接，先读现有 `run.yaml`、`status`、`check` 和请求/会话记录，核对真实原生进程及容器；不得为“继续”自动新建另一批或重复启动已有会话。

## 1. 明确本次有效预算与配置

从用户请求、继承配置和历史授权提取已知值，只问会影响此次执行的缺项。新问题/新 study 不自动继承旧项目的次数、模型或最终授权。用户已给定范围并要求开始时直接执行；单纯询问用法或创建 Skill 不构成研究授权。

启动前给出简短有效配置摘要，至少包含下表。未知预算给出有依据的小规模建议并取得答案；用户明确同意使用指定配置时不再逐项确认。

| 配置 | 必须明确的值与含义 |
| --- | --- |
| 身份与模块 | STUDY、下一编号批次或待续接 RUN_ID、基础配置、八个具体方法绑定、purpose |
| 模型 | research 实际 profile 的 model/reasoning_effort；默认 gpt-6.1-sol/high，新上下文 fork_turns=none；实际可用的并发能力 |
| 会话 | `researchers`、`rounds_per_researcher` 及其乘积；轮数是会话数，一轮可多次改代码评测 |
| 时间 | `session_wall_s` 默认 2400 秒有效研究时间：从实际激活开始，扣除评测前置检查、排队及执行；到期只准收尾，不杀进程。`run_wall_s` 默认 345600 秒（96 小时），从 init 起计算整批新工作准入窗口 |
| 评测容量 | `evaluation_calls: null` 默认不限调用次数；`max_cases_per_dataset: null` 不设统一容量上限，实际集合数量已在初始化与用户确定。`probe_calls` 是单独探测授权，默认 0，普通开发集评测不受其限制 |
| 算力 | executor、固定镜像、默认三槽、整次 `evaluation_wall_s` 及单例 limits 的 wall_s/cpu_s/memory_mb/pids/output_bytes/tmp_mb；初始化按任务建议并向用户说明。交互动作时限和 max_steps 来自已封存 benchmark |
| 终局 | `final.authorized` 的真实授权、final 集标识与规模、冻结选择规则、最终方法范围、报告/图表 |

当前后端仅 local-docker、最多三槽且每个候选容器单核、无网络/GPU；模型并发与评测槽位是两件事。实际预检核对 Docker 可用 CPU/内存及其他项目占用，资源不足时明确处理，不能假称三槽已运行。若原生工具不能满足已配置模型或资源，说明缺口，不默默换模型/预算，也不把创建会话记录当作模型已启动。

一次评测调用覆盖整个指定数据集，并非按案例计一次。默认调用次数不限，不为某轮人为预分配小额度；每次仍有从取得槽位开始计算的整次执行预算，排队另列，单案例计算限额保持不变。用户显式设置有限 evaluation_calls 时才计算基线、研究和最终预留；probe 同时占总额度和 probe 子额度。默认 final 每个集合需基线和固定候选各一次。96 小时准入窗口仍需容纳终局。

仅在有限调用额度下：**当前程序没有 final 专用硬保留额度**，`remaining_evaluations` 是全部余额。把最终预留与研究员分配写入启动提示，这是调度约定，不能宣称程序已强制。默认 null 表示不限次数而非 0，不需要这种分配。不添加 schema 不支持的键。额度或总时限不足时报告未完成部分，不擅自加预算。

## 2. 建立或复用编号批次

基础 `configs/*.yaml` 为 schema_version 5，保存共享默认值；`runs/STUDY/NNN/run.yaml` 为 schema_version 8，只写 extends、initialization 引用及本批覆盖。不复制整份有效配置、公共 Skills、模块说明或共享环境。字段结构见 [schemas.json](../../../tools/schemas.json) 的 run_document；已有批次引用的基础配置不可原地更改。

新批次的预算都继承且只有人数/轮数需覆盖时：

```sh
.venv/bin/python -B tools/run.py init STUDY --config configs/STUDY.yaml --researchers N --rounds R
```

其他预算/资源/授权需覆盖时，选择未占用编号，先准备**只有 run.yaml 的草稿目录**，填写合法稀疏覆盖，再执行 `tools/run.py init STUDY/NNN`，不加 `--config`。初始化后不得手改冻结配置。所有后续命令使用实际返回的 RUN_ID，不假定为 001。续接已有批次不重复 init，也不重新封存 study。

```sh
.venv/bin/python -B tools/run.py config RUN_ID
.venv/bin/python -B tools/run.py check RUN_ID
.venv/bin/python -B tools/run.py status RUN_ID
```

核对解析后的配置、镜像/依赖、Docker 响应及其他任务资源占用。在任何模型启动前，通过 `tools/measure.py --run RUN_ID --candidate baseline --dataset ANCHOR_ID --evaluation baseline-dev` 完成计账的基线预检；已完成的同批记录直接检查，不重复求解。核对合法率、手算规则与难度是否有辨别力；失败、指标失真或明显不可承受的预算先处理，不带病开启研究，也不原地修改已封存环境。

## 3. 通过 research 启动和推进

按选定 research 包执行，不把所有轮次一次性登记为 started。当前方法的首轮入口：

```sh
.venv/bin/python -B tools/run.py begin-session RUN_ID --actor r1 --module research --role researcher --idea IDEA_ID
```

按返回的 `spawn_options` 真正调用原生 subagent，交付 RUN_ID、session_id、idea/iteration、所选 `RESEARCHER.md`、授权开发材料及有效停止边界。启动成功后立即 `tools/run.py activate-session RUN_ID --actor r1 --native-id ACTUAL_ID`，从此开始累计有效研究时间；重复激活不会重置计时。`begin-session` 只是登记，不能代替实际模型调用。若没有真实启动成功，按实际失败处理，不能留下伪造的成功记录。

研究员自行写算法代码，提交当前接口要求的候选 manifest，直接调用 `tools/research.py` 完成 evaluate/result、笔记、共享发现、复用及下一代算法想法。主会话不代写候选，不逐次转发评测，不为赶轮数伪造调用。源码与 manifest 的格式以 research 包为准；不要捏造当前不存在的仅 `--source` 提交接口。

按模块检查 context.research_time 和真实请求；不能用注册以来的墙钟时间或固定 40 分钟计时器杀进程。等待在途工具返回，排队/执行不计有效研究时间。达到有效时限后拒绝新评测，研究员只做结果读取、iteration、checkpoint 和结束说明；没有额外收尾杀进程时限。确认原生会话已结束、无未决请求后 `tools/run.py end-session RUN_ID --actor r1 --status completed --native-id ACTUAL_ID`；真实失败用 failed。下一会话使用其 notebook 指向的 `--iteration ITERATION_ID` 和全新上下文，各 actor 独立推进。

暂停、异常或中断时保留实际状态；先查原生进程、请求、attempt/result、容器和锁。不能通过改请求 ID、补写成功、重放已启动求解或额外会话掩盖故障。处置不明确时向用户说明具体待决定事项。后台自动化仅在用户要求时设置，不因 Skill 启动就自建监控任务。

## 4. 冻结、最终评测与交付

先确认所有会话和请求结束，执行：

```sh
.venv/bin/python -B tools/research.py audit RUN_ID
.venv/bin/python -B tools/run.py check RUN_ID
.venv/bin/python -B tools/run.py status RUN_ID
```

审计返回 ok 不等于无在途，必须查看 pending_requests 等未决项。按 final 包规则从完整合法、非 probe 的 anchor 成绩选择候选；没有合格候选就报告事实。默认通过 `tools/run.py select RUN_ID --candidate CANDIDATE_ID` 冻结真正最优候选及源码，不能按个人偏好改选。

真实最终授权已具备时不再次询问，核对 final 数据早已封存、额度与时限充足，然后主会话调用：

```sh
.venv/bin/python -B tools/measure.py --run RUN_ID --candidate baseline --dataset FINAL_ID --evaluation baseline-final
.venv/bin/python -B tools/measure.py --run RUN_ID --candidate CANDIDATE_ID --dataset FINAL_ID --evaluation candidate-final
```

多个 final 集需不同 evaluation ID。当前终局只测基线和冻结候选；不擅自扩大为所有候选、多种子或新实例。最终分数不回流研究，不凭最终表现改代码、换候选或重跑失败。只有研究授权时输出研究报告，明确未做最终测试；不能把模板里的 true 当作用户授权。

主会话按 final/visualization 包准备报告、图及各自 manifest，再分别用 `tools/run.py deliver RUN_ID report ... --module final` 和 `deliver RUN_ID figure ... --module visualization` 登记。不存在自动生成报告的 `run.py final` 命令。

最终核对算法源码、输入与指标、原始结果、合法率、失败、实际模型调用和求解/排队开销；完整均值只用于全合法结果，配对子集标分母。报告假设和适用限制。交付 RUN_ID、有效配置摘要、冻结代码、报告和图的链接，以及完成/失败/未完成状态；不要以“命令跑完”代替研究与终局检查完成。
