# 研究目录

```text
runs/<study>/
  initialization/{task,datasets,ideas}/   # 同一 study 共享的封存输入
  cache/                                  # 可重建的下载或预处理文件
  001/
    run.yaml                              # 本批次配置覆盖
    state.json                            # 配置快照与批次状态
    events.jsonl
    research/{candidates,evaluations,selections,exchange}/
    final/
    visualization/
runtime/
  locks/                                  # 各 study 共用的资源锁
  jobs/<study>/001/                       # 该批次的在途作业及协调锁
```

首次执行 `init STUDY --config configs/STUDY.yaml` 创建 001。重复 init 打开已有批次并校验绑定；要复用相同初始化开展独立实验，显式传 `--new-batch`。每个新批次在 state.json 中保留基础 YAML 原文，并校验其摘要。修改配置模板后，旧批次继续读取自己的版本。任务或初始化变化时创建新 study。

准备脚本使用 `tools.common.study_cache(study)` 定位缓存。需要核验的输入、评测记录和完成回执应登记在初始化或编号批次中。项目级锁按资源共享；等待所有相关作业及恢复操作结束后再清理临时文件。
