# AnySolvers

模块化的问题求解研究框架。研究员在原生会话内编写算法、直接评测和改进；主会话负责初始化、会话启动、预算、异常和最终检查。

本仓库保存当前 v2 实现；本地开发目录仍可命名为 `AnySolvers-v2`，以下命令都在仓库根目录运行。

## 本地安装

使用 Python 3.10 或更新版本：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -s tests
```

默认测试不调用研究模型，真实 Docker 测试需显式设置 `ANYSOLVERS_V2_DOCKER_TEST=1`。原生研究员由支持相应模型和 subagent 的 Codex 主会话启动。

当前沙箱主要在 macOS + Docker 环境验证，依赖 POSIX 文件锁、定时器及 `/private/tmp`。评测使用代码与配置中固定的 Docker 镜像 ID；仓库尚未提供该镜像的分发地址或 Dockerfile。在其他机器开始真实评测前，需要先准备并核验对应执行环境；只安装 Python 依赖并不能获得完整沙箱。运行数据和虚拟环境默认不纳入 Git。

## 模块类别与具体方法

`modules/` 第一层是八个固定职责，第二层是可替换的具体方法。类别目录不放某个方法的实现，名字描述机制。

| 部分 | 类别 | 当前方法 | 内容 |
| --- | --- | --- | --- |
| 初始化 | task | [problem_spec](modules/task/problem_spec/MODULE.md) | 问题、约束、目标与信息边界 |
| 初始化 | datasets | [stratified_holdout](modules/datasets/stratified_holdout/MODULE.md) | 仿真/真实数据/生成实例与评测接口初始化 |
| 初始化 | ideas | [diverse_hypotheses](modules/ideas/diverse_hypotheses/MODULE.md) | 有区别、可验证的初始假设 |
| 循环 | research | [notebook_collaboration](modules/research/notebook_collaboration/MODULE.md) | 多研究员、笔记续接、共享发现和源码 |
| 循环 | evaluation | [queued_sandbox](modules/evaluation/queued_sandbox/MODULE.md) | 调用已初始化环境的运行时沙箱评测 |
| 循环 | selections | [evidence_iteration](modules/selections/evidence_iteration/MODULE.md) | 根据证据提出下一代算法想法 |
| 终局 | final | [frozen_holdout](modules/final/frozen_holdout/MODULE.md) | 冻结选择、最终测试和报告 |
| 终局 | visualization | [evidence_charts](modules/visualization/evidence_charts/MODULE.md) | 基于已有测量的图表 |

```text
modules/
  research/
    notebook_collaboration/
      MODULE.md
      RESEARCHER.md
      client.py
      runtime.py
      sessions.py
      schemas/
  evaluation/
    queued_sandbox/...
  ...其余六个类别...
spare_modules/
  datasets/
    feedback_challenges/...   # 完整备用方法，不能直接绑定运行
configs/smoke.yaml            # use 指向具体方法目录
tools/                       # 主会话工具及通用命令入口
runs/STUDY/
  initialization/{task,datasets,ideas}/
  001/run.yaml
  002/run.yaml
```

备用区目前有 `datasets/feedback_challenges`，用于候选反馈驱动的困难实例。它包含自己的说明、校验代码和格式定义，不借用在用方法的文件。更换时移动完整目录并修改 `modules.<类别>.use`，详见 [MODULES.md](docs/MODULES.md)。

## 启动与研究员接口

项目提供两个显式调用的 Skills，放在 `.agents/skills/`，只引用现有模块，不复制到运行目录：

| 入口 | 职责 | 主要产物 |
| --- | --- | --- |
| [$anysolvers-initialize](.agents/skills/anysolvers-initialize/SKILL.md) | 交互补齐需求、数据与资源，准备 task/datasets/ideas 并封存共享初始化；默认不启动模型或评测算法 | `configs/STUDY.yaml`、`runs/STUDY/initialization/` |
| [$anysolvers-research](.agents/skills/anysolvers-research/SKILL.md) | 明确本批预算与模型/算力配置，启动研究员直接评测的循环，冻结候选后完成获授权的终局和图表 | `runs/STUDY/NNN/run.yaml`、研究代码/记录、最终报告与图 |

在以 **AnySolvers-v2 为工作目录**的 Codex 会话中点名调用。例如：

```text
使用 $anysolvers-initialize。我想解决……，现有数据在……，使用本地算力；先与我补齐信息，只准备工作环境。
使用 $anysolvers-research。复用 study ……；明确本批预算后，开始研究并完成最终测试。
```

两个入口设为显式调用，单纯讨论框架不会自动启动。未在当前会话列表出现时，可先直接让主会话读取上述 SKILL.md；在父目录打开的会话不会因此自动加载子项目技能。项目目录发现与调用方式遵循 [OpenAI 官方 Skills 文档](https://learn.chatgpt.com/docs/build-skills)。没有全局安装或额外 MCP 服务。

人数/轮数与时限等易变值在编号批次覆盖；模型、环境等默认值从基础配置继承。默认评测次数不限；显式设置有限额度时需计入基线和最终测试。`final.authorized` 必须对应用户授权。初始化检查与实际沙箱基线测试分开，后者在研究启动阶段计账执行。

先看 [START.md](START.md)。`configs/smoke.yaml` 是轻量模板，默认 gpt-6.1-sol/high、2 位研究员各 2 会话；`run.yaml` 继承并按次覆盖预算。每轮默认 **40 分钟有效研究时间**，扣除启动等待、评测前置检查、排队和执行。到期禁止新评测，保留笔记与收尾接口，不杀原生进程。整批准入窗口为 **96 小时**，评测次数不限，默认 **3 槽**。模板引用 `tests/fixtures/tiny-knapsack/task.json`，这是任务样例，不是实验成绩。

开发/最终集合大小在初始化时与用户确定，不默认限定为 12 例。单例计算和整次评测时限由初始化按环境、实例规模、可用资源及已有公开基线测量给出建议并告知用户；样例的 10 秒单例预算不能照搬到新任务。每例限额固定，整次时间不足以容纳完整下一例时不再启动它。

资源建议入口：`.venv/bin/python -B tools/plan_resources.py STUDY --config configs/STUDY.yaml`。它只读协议与集合数量，不执行候选、不自动改配置；可带公开基线测量作为依据。主会话说明建议后将合适值写入配置，再封存并启动。

```sh
.venv/bin/python -B tools/research.py evaluate STUDY/001 --session r1-r001 --manifest research/candidates/c1/manifest.json --id req-c1 --dataset anchor
.venv/bin/python -B tools/research.py result STUDY/001 --session r1-r001 --id req-c1 --case anchor-01
.venv/bin/python -B tools/research.py findings STUDY/001 --session r1-r001
```

同一会话可多次评测；默认返回摘要，逐例详情按需读取。笔记、发现、复用和下一代算法想法由研究员直接登记，主会话不逐次转发。完整接口见 [COLLABORATION.md](docs/COLLABORATION.md)。

新运行记录放在 `research/{candidates,evaluations,selections,exchange}`；exchange 保存请求、响应、笔记、发现和复用来源。公共提示词、Skills 和完整聊天不复制进每次运行。旧路径、上一轮验收记录及本次迁移兼容层不保留。

## 边界与检查

候选在固定 Docker 中运行：单核、只读根盘、无网络/GPU和固定资源限制。预算不会因直接调用扩大，未决尝试不自动重跑。原生 agent 共享宿主文件权限，工具访问规则不是角色级 OS 隔离。

`tools/run.py launch-spec --config configs/smoke.yaml --module research --role researcher` 只展示启动参数，不启动模型。结构和接口检查：`.venv/bin/python -B -m unittest discover -s tests`。测试 mock 不代表真实算法成绩。

## 评测初始化在哪里

task 只产出 task.md / task.json；datasets 根据定义准备可执行评测包：benchmark.json、可信评分器或仿真适配器、基线、规则 fixtures，以及实例清单与真实数据来源。evaluation 仅装载并运行这套包。详见 [评测包接口](modules/datasets/stratified_holdout/MODULE.md)。

支持静态 solve(case) 和交互 solve(observation) 两种模式。真实数据通过明确的原始文件与适配器导入，缺失数据不会自动换成合成数据。共享环境在 initialization/datasets 中只存一份，各数据划分引用它；编号批次新增 challenge 可以引用 @anchor。当前交互执行器用于轻量可信 Python 环境与固定策略评测，尚不包含训练调度或通用大型仿真后端。
