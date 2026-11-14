# 迁机与故障复跑

一个编号对应一次实验。迁机时带上本地项目资产、基础配置和原 study 目录（含 initialization 和要继续的编号）。例如原批次为 study/003，在新机器的项目根目录运行：

```sh
.venv/bin/python -B tools/run.py resume study/003
.venv/bin/python -B tools/run.py status study/003
```

恢复会核对原输入、交付、历史摘要和案例日志，并采用当前 run.yaml、基础配置和本地执行器。重复恢复沿用原编号、会话、评测额度和创建时间。直接执行 init STUDY 会复用最近已初始化的编号。需要独立对照时用 `init STUDY --config configs/STUDY.yaml --new-batch`。

## 评测中断

- 只丢失本地提交或 SSH 回执时，运行 `tools/run.py reconcile-evaluation RUN --evaluation ID`。它读取原作业、补登记已有结果。SSH 使用提交时记录的目标。
- 基础设施出错或只完成一部分时，运行 `tools/run.py retry-evaluation RUN --evaluation ID`。它在原编号和评测 ID 下先取回已有结果，再用当前算力补算。已落盘、案例身份匹配的连续完整结果可复用。候选、案例限额或评分环境不同的结果分别计算。候选给出的无效解也算已评测。旧摘要和日志保留在原位置，重试只存新增记录。用 `tools/measure.py --run RUN --details ID` 查看合并后的结果。
- 旧云主机永久失联时，确认原作业已停止后，可给 retry-evaluation 加 `--remote-stopped`。此时从本地已有的完整结果继续。本地从未收到的远端结果需要重新计算。
- 研究请求或笔记落盘时中断，用 `tools/run.py reconcile-exchange RUN` 补登记。评测已封存而 response 缺失时，研究员用原请求 ID 再次调用 evaluate。
- 修复单批次的评分器时，用 `tools/run.py resume RUN --scorer PATH.py`。替换作用于使用原 anchor 评分器的集合（含引用它的 challenge 和 final）。修订文件存一份，原初始化和已封存报告保留。命令列出的 stale_evaluations 是需要重新评分的 ID。对这些 ID 运行 retry-evaluation 后读取当前成绩。

## 会话与预算

原生进程仍在时继续原会话。进程丢失时按该会话的启动参数启动接替进程，执行 `tools/run.py resume-session RUN --actor ACTOR --native-id NEW_ID`，再用原 session_id 读取 context。笔记、请求、轮次和已用额度随会话保留。知道停机时间时，可加 `--stopped-at 2026-11-13T08:00:00+08:00` 扣除这段研究员停机时间。

批次准入窗口仍从最初初始化时开始计算。长时间停机导致窗口耗尽时，可在原批次 run.yaml 调整已授权的 budgets.run_wall_s，再运行 resume。SSH 目标可通过环境变量 ANYSOLVERS_SSH_HOST 更换。

原研究起点或方法报告只写出文件、尚未登记时，使用原参数重提即可核对已有文件。硬盘损坏时，能恢复的案例取决于迁移副本或备份中实际存在的日志。
