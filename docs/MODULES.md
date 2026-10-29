# 方法包的组织与替换

## 三个层次

1. **类别**：八个稳定职责，例如 research、evaluation。类别本身不是具体实现。
2. **方法**：`modules/<类别>/<方法>/`。一个目录是一套可替换的完整实现，例如 `research/notebook_collaboration`。
3. **运行**：`runs/<研究名>/<编号>/`。保存参数和研究产出，不把本轮内容写进方法包。

`spare_modules/<类别>/<方法>/` 存放未启用方法。格式与在用包相同，但运行时拒绝直接绑定备用路径。配置中的 use 决定本次选择，不根据目录名猜测，也不自动执行备用区。

## 方法包包含什么

- `MODULE.md`：唯一入口。元数据声明 id、kind、输入输出、handler、依赖文件和参数；正文描述执行步骤。
- `schemas/`：交付与参数的明确格式。
- 必要的 Python 工具：可信检查、评分调度、记录等确定性动作；不需要程序的模块可以只含说明和格式。
- 研究方法需要时带 `RESEARCHER.md`：研究员的初始上下文。

声明的依赖必须在同一个方法目录内，不能通过 `../` 借用兄弟方法或备用区的文件。通用入口仍在 tools。不同类别通过配置绑定的 handler 通信；同一类别不自动保证两种方法可互换，还需要匹配输入输出和操作语义。

当前组合使用 datasets 的评测包登记/环境加载、research 的准入和记录审计、evaluation 的预检/评测/详情、selections 的证据读取/迭代交付。替换时应保留所需操作并做行为测试。只有 research 管理 subagent，其余方法不会另起模型。

## 更换方法

先确认没有使用相关方法的在途研究或评测。通常在新运行开始前移动完整目录，更新 `modules.<类别>.use`，然后检查配置。不要只移动 MODULE.md，不用软链接保留旧入口。

```yaml
modules:
  research:
    use: modules/research/notebook_collaboration
  evaluation:
    use: modules/evaluation/queued_sandbox
```

例如启用反馈驱动的困难实例构造：将 `spare_modules/datasets/feedback_challenges/` 整包移到 `modules/datasets/feedback_challenges/`，再绑定。原方法在不再被使用后整包移入备用区。

**注意用途差异**：feedback_challenges 只生成研究阶段 challenge，不能替代初始 anchor/final。若在已封存初始化之后使用它，应在没有活动会话或未决评测的边界按 bind 的规则切换：

```sh
.venv/bin/python -B tools/run.py bind STUDY/001 datasets modules/datasets/feedback_challenges
```

共享初始化仍依赖其固定的数据格式定义，不能在依赖尚存时移走所需包。因此仍被不同运行或初始化使用的两种方法都可以在 modules 下；完全闲置的才进备用区。task 不能中途更换，evaluation 在首次测量后不能更换。常规对比优先使用新编号批次，任务/初始化协议变化时新建研究名。

只改人数、轮数和资源预算时，在新批次 run.yaml 覆盖参数即可，不必创建新方法目录。

## 命名与记录

目录用小写机制名，例如 notebook_collaboration、queued_sandbox、evidence_iteration；MODULE 的 id 对应连字符写法。思想变化使用新方法名，本轮参数留在 run.yaml。

不保留旧路径别名、迁移映射或历史哈希档案。当前运行仍记录输入、源码和配置哈希，以发现运行过程中意外改动；这是一致性检查，不是旧路径兼容。

## 初始化与运行时的边界

task 只定义问题，不保存可执行评分代码。datasets 负责把问题落实为评测包：现成真实数据的来源与转换，或实例生成与仿真环境，以及评分、基线、规则检查。evaluation 只消费已登记的评测包，执行候选、计时和记录结果。

替换 datasets 方法需提供 benchmark、source 和实例的格式以及 load_environment 接口；单纯更换实例生成策略可以复用已有 benchmark。改变世界动力学、观测边界或评分规则应建立新的共享初始化，不能当作同一批次的额外实例悄悄替换。
