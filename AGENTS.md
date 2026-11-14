# AnySolvers-v2：入口与边界

八个模块类别平级，具体方法放在 modules/<类别>/<方法>/。当前研究方法为 notebook_collaboration：笔记续接与共享发现协作，研究员直接调用工具。未启用方法整包放在 spare_modules/<类别>/<方法>/，不可直接绑定运行。

先读 README.md、选定配置和所选 research 方法包的 MODULE.md。START.md 是启动教程；该 MODULE.md 是唯一循环说明，包内 RESEARCHER.md 是研究员的调用说明。模块替换见 docs/MODULES.md。

研究流程的显式入口为 [.agents/skills/anysolvers-initialize/SKILL.md](.agents/skills/anysolvers-initialize/SKILL.md) 与 [.agents/skills/anysolvers-research/SKILL.md](.agents/skills/anysolvers-research/SKILL.md)，分别负责交互初始化、明确批次配置并启动研究与终局。领域整理另有显式入口 [$anysolvers-dream](.agents/skills/anysolvers-dream/SKILL.md)。Skills 引用模块，不替代模块规则或复制到 runs；阅读入口本身不授权启动实验。

- 主会话通过 research 启动/结束/续接原生研究员，设置预算、处理异常、冻结最终候选并做终局检查；初始化 task/datasets/ideas 与最终评测、报告及图表由主会话完成；终局 wiki 由 final/dream 的专用子 agent 编写。
- research 委派研究员；final/dream 委派一位 wiki 写作者。其他模块是主会话或研究员调用的确定性程序，不启动模型。execution.mode: main 表示无独立模块 agent，不表示研究员必须请主会话转发工具。
- 研究员自行写算法，在同一会话直接调用 tools/research.py evaluate/result/findings/share/reuse/checkpoint/iteration/context。评测、发现、复用、笔记不需要主会话逐次审批；下一代算法想法由研究员提出。
- 原生参数从 begin-session 返回值取得；默认 gpt-6.1-sol/high，fork_turns=none，2 位研究员各 2 会话。真实启动后立即 activate-session 绑定 native_id，排除启动等待。每轮默认 40 分钟有效研究时间，扣除评测前置检查、排队和执行；到期禁止新评测但允许读结果、写笔记与收尾，不杀进程。新会话读自己的封存笔记和迭代，不复制整段旧对话。工具不调用模型，不能用配置代替实际调用证据。
- 研究员可直接在本地运行和调试候选草稿。正式候选与基线经统一评测入口计账。项目资产保存在本地，设置 ANYSOLVERS_SSH_HOST 时优先提交到该主机，否则使用本地 Python。每例和整次评测分别限时，默认三槽，排队时间单列。实例数量与用户确定。
- tools/run.py、tools/measure.py 是协调者管理入口；研究员只用 tools/research.py 的会话接口，不调用最终评测/选择/改配置/改状态。身份是会话协议约束，不是宿主权限隔离；不能声称 final 或其他目录在 OS 层不可读。
- 研究员可读自己的历史、开发输入、基线成绩、已发布发现及其证据；不可读他人私有笔记/未公开代码、最终输入/成绩或旧项目。公开发现的自然语言解释与可信分数分开；复用前直接调用 reuse 登记来源，不等人工批准。
- 每请求唯一 ID，自动封存候选、原子预留预算，程序排队测量，返回摘要；详情按需读取。重复调用同请求不重复求解；未完成 attempt、基础设施错误或清理未知阻止新测量，保留现场交主会话处理，不能改 ID 绕过。
- runs/STUDY/initialization/{task,datasets,ideas} 是共享初始化；runs/STUDY/001 等编号批次各自持 run.yaml。config 是继承基础，批次只保存 extends、初始化引用和覆盖。迁机、配置或执行器修复沿用编号，修改 run.yaml 后用 resume 登记。原始输入、交付、历史成绩保留。评分器修复和中断续跑见 docs/RECOVERY.md。独立实验用 init --new-batch。
- 最终数据先封存，研究结束后固定候选才测量；反馈只进入报告和可视化，不回流研究。全合法才报告全体质量均值。
- 不访问凭据，不干预其他项目，不启动正式研究、后台监控或增加预算，除非用户授权。当前授权仅改造与轻量验证。
- 不安装全局 Skill 或 MCP 服务；本地 CLI 已能直连，未来可包装同一接口。assets 不参与工作链，公共说明不复制到每个运行。

- 方法依赖只能在本包内声明；不把具体实现平铺到类别目录，不用外部框架名命名当前思想。旧路径别名、历史哈希档案和旧运行记录不保留，不为此次目录修改启动研究。
