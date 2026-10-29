# 运行目录

```text
runs/<研究名>/
  initialization/{task,datasets,ideas}/
  initialization/manifest.json
  001/
    run.yaml
    state.json
    events.jsonl
    research/
      candidates/
      evaluations/
      selections/
      exchange/{request,response,notebook,finding,reuse}/
    final/
    visualization/
  002/...
```

同研究的批次复用初始化，按数字编号。方法实现放在 modules，这里保存初始化和本次运行的输入、参数、候选及结果。当前没有已启动研究或保留的旧批次。
