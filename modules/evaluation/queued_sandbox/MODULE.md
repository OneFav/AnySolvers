---
version: 4.0.0
execution:
  mode: main
  delegates: {}
defaults:
  trace_retention: summary
parameters: schemas/params.json
capabilities:
  short_state_transactions: true
  trusted_feedback: true
id: queued-sandbox
kind: evaluation
accepts:
- run_config
- task
- dataset
- candidate
- selection
produces:
- evaluation
contracts:
  evaluation: schemas/evaluation.json
handlers:
  evaluate: measure.py
  reconcile: measure.py
  details: measure.py
  preflight: measure.py
  evaluation_active: measure.py
  resource_contract: measure.py
files:
- sandbox.py
- retention.py
---

# 可信三槽评测

研究员直接通过 tools/research.py evaluate 调用；主会话仅在准备基线、异常处理和最终阶段用 tools/measure.py 管理入口。程序负责评分，不允许研究员写正式分数。

输入为已封存候选、开发集、会话、请求ID与固定预算。提交时原子预约预算；实际开始时再次检查会话/源码/数据/资源。默认最多三个数据集评测并行，每个槽内逐例运行，每个候选容器仍为单核。槽位等待不占状态锁，admission_queue_wait_s 与沙箱 queue_wait_s 单列；启动等待、评测前置检查、排队和执行均不消耗研究员的有效研究时间。测量运行中其他 actor 可登记笔记、发现和新候选。

`compute.evaluation_wall_s` 是取得槽位后整次数据集评测的时间预算；排队不计入。每个新案例只有在剩余额度能容纳完整单案 wall_s 与准备/清理预留时才启动，不缩短最后一例的原计算限额；不足则保留已完成结果并明确报告未运行案例。单案 wall_s/cpu_s/内存等由初始化按任务推荐，向用户说明后写入配置。`resource_contract` 返回执行器的开销预留，供初始化计算整次预算；该预留不是给算法增加的计算时间。96 小时整批窗口在每次新案例开始前检查，到期让已运行案例按原限额结束、停止后续案例，不强杀原生研究员。

候选及基线只在固定 Docker 镜像中执行，非root、只读根盘、无网络/GPU、单核与内存/进程/输出限制；宿主仅导入可信评测器。结果含所有 attempt/result，未知耗时不作0。全体合法、清理与基础设施正常才有总体均值。

开发结果在 research/evaluations，最终在 final/evaluations。默认返回摘要，研究员 result --details/--case 按需读取；接口只开放自身与公开开发证据。最终仍需固定选择与用户授权，研究员直接接口一律拒绝。

同请求不重复求解，同一快照请求互斥。三槽由本项目共享锁限制，正在运行的评测持有独立租约，不能把其他槽的真实在途记录误判为孤儿。中断记录、缺失结果、未知清理或基础设施错误保留并阻止后续测量；主会话先核验现场，不改ID绕过。锁由工作进程持有，不启动后台服务。evaluation_calls 默认为 null，即不限制会话或批次的评测次数；设置有限值时为全批共享，排队请求也占预约容量。probe_calls 仍是单独的探测授权，默认 0；它不限制普通开发集评测。

## 输入的可执行评测包

评分器、基线、实例与仿真规则均由 datasets 初始化并登记；本模块不创建数据、仿真器或评分目标。运行时通过所选 datasets 的 load_environment 接口加载已封存的 benchmark，并核对文件。batch challenge 的 @anchor 引用复用共享环境，主会话无需再复制评分器。

- function 协议：每个实例在新容器调用一次 `solve(case)`，由可信评分器核验返回解。
- episode 协议：每个实例新建一个持久策略容器，多次调用 `solve(observation)` 返回动作；同一 episode 内可保留进程内记忆。可信环境在宿主执行 reset/step/score_episode，只有当前 observation 发送给候选。候选没有环境代码、完整私有实例或未来事件文件挂载。环境自身必须正确过滤观测，接口不能替错误的环境定义保证未来隔离。

交互协议受 max_steps、每动作 action_wall_s、整例 wall_s 以及原单核/内存/CPU/输出上限约束；响应超时或非法动作无分数，未知清理或基础设施错误阻止继续测量。静态评分及交互可信 Python 回调使用主线程 POSIX 定时器控制，计入单案时限；三槽通过三个独立进程执行，不能用子线程替代。当前不支持不可中断的原生仿真引擎。环境运行消耗宿主资源，不包含在候选容器的内存/单核限制内，重型环境须另行配置执行器，不能将该限制宣称为全环境资源上限。

每例详情可包含交互 trace（观测、动作、下一观测、终止标记）；默认仍只返回摘要，详情按需读取。研究员接口、轮数与预算管理不变；候选 manifest 中 solve(case) 的 case 在 episode 协议下表示本步 observation。

## 轨迹留存和完成回执

新批次默认 `modules.evaluation.params.trace_retention: summary`。可信评分器先获得完整 trace 并完成评分，之后才裁剪持久化结果：保留分数、全部 metrics、资源与清理信息、attempt/result 身份，逐步轨迹改存规范 JSON 哈希、字节数与记录数。失败最多保留最后三步、合计 4KiB 的片段；stdout/stderr 各保留至多 4KiB 的首尾内容（另加截断标记），附原长度与哈希。留存策略进入请求身份。

需要完整轨迹时，在封存新批次之前将参数设为 `full`；诊断输出仍有界。summary 模式没有完整轨迹展开入口，不能为补日志自动重跑。旧封存结果不被改写。

执行前持久化逐例 attempt，结果返回后持久化 result。整次结束先落 `completion.json`（固定配置身份、完整摘要与逐例日志哈希），再发布摘要和状态封印。进程若在最后提交期间中断，主会话使用 `tools/run.py reconcile-evaluation RUN --evaluation ID`：取得原评测租约、校验配置与原请求、完整 attempt/result 配对和已确认清理后只补状态。缺失/损坏回执、未完成 attempt、基础设施错误或未知清理继续阻断；接口不会重新求解或推测遗失成绩。原请求再次读取已封存结果时补齐 response。
