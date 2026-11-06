# 启动教程

唯一科研循环规则在 modules/research/notebook_collaboration/MODULE.md；这里仅说明准备和调用顺序。

1. 与用户确定任务、计算资源、开发/最终实例数量与最终测试授权。基于 configs/smoke.yaml 准备配置；默认人数/轮数仍为 2×2，40 分钟有效研究时间、整批 96 小时准入、评测次数不限、三槽。数量没有统一上限，必须按本次任务明确，不因准备文件而自动获得正式研究授权。
2. 在 runs/STUDY/initialization/task/ 准备 task.json、task.md，写明问题、约束、目标和观测边界；该目录不放评测程序。
3. 先 `tools/run.py study-init STUDY --config configs/CONFIG.yaml`。datasets 准备 benchmark.json、评分器或仿真适配器、基线、规则 fixtures、实例清单与数据来源；可接入指定真实数据。先审阅可信代码并核对手算规则，再登记；ideas 继续按原模块准备。`study-deliver STUDY dataset datasets/anchor/manifest.json --module datasets` 等逐个登记。最终集预先确定，不交给研究员。
4. 登记全部集合后，运行 `tools/plan_resources.py STUDY --config configs/CONFIG.yaml`，结合任务和已有公开基线数据选择单案/整次预算，告知用户并写回配置；没有实测的建议会明确标为启发式。`tools/run.py study-seal STUDY` 封存后，再 `tools/run.py init STUDY --config configs/CONFIG.yaml --researchers 2 --rounds 2` 创建下一编号。后续命令都使用返回的 STUDY/001。初始化前也可先写只有 run.yaml 的编号草稿；初始化后只能通过已授权边界改动，不能直接改冻结文件。
5. 主会话先检查 Docker 与资源；可用 `tools/measure.py --run STUDY/001 --candidate baseline --dataset anchor --evaluation baseline-dev` 测基线。它是协调者入口，研究员不用它。
6. `tools/run.py begin-session STUDY/001 --actor r1 --module research --role researcher --idea IDEA` 返回准确模型与强度。主会话按返回参数真实 spawn 新上下文，随后立即 `tools/run.py activate-session STUDY/001 --actor r1 --native-id ACTUAL_ID`。交付 RUN_ID、session_id、idea/iteration、授权材料、RESEARCHER.md 和停止边界。登记到实际启动的等待不计时。
7. 研究员直接使用 tools/research.py 完成所有开发实验与记忆/分享，不需要主会话转发。在同一原生会话内可多次修改代码并评测。context.research_time 显示扣除评测前置检查、排队和执行后的余额；40 分钟用完后只允许收尾，不杀进程。结束前保存 iteration（有后续会话时）及 checkpoint，并确保请求全部回应。
8. 主会话核对原生会话已结束再 end-session，按 selections 的方法报告接口核对源码并封存该轮报告（最后一轮也需要）。若该 actor 还有预算，通过其 notebook 指向的 iteration 启动新上下文；不同 actor 可独立推进，无整组屏障。
9. 所有会话与测量结束后 audit、check，再由 final 模块选定并冻结候选。仅在最终测试已授权时评测基线及固定候选；独立检查原始解、合法率、失败和成本，保存报告和图。不要将 mock 单测当作模型/沙箱验收。

所有命令前加 `.venv/bin/python -B`。研究员直接工具的完整参数见 `tools/research.py --help` 和 modules/research/notebook_collaboration/RESEARCHER.md。工具进程运行中只等待同一进程；断开或异常后先读 result 和原始记录，禁止改 ID 重试未决求解。
