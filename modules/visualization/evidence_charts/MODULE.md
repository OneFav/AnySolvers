---
id: evidence-charts
version: 2.0.0
kind: visualization
accepts:
- run_config
- task
- evaluation
- selection
- report
- authorized_inputs
produces:
- figure
execution:
  mode: main
  delegates: {}
contracts:
  figure: schemas/figure.json
handlers:
  validate_delivery: validate.py
files: []
defaults: {}
capabilities: {}
---

# 结果可视化

将 final 已整理或其他明确授权的测量证据转换为图表。此模块在最终证据就绪后执行，可按需省略，不新增测量。

## 输入与文件

输入为任务目标方向、授权 evaluation/selection/report、展示要求。输出 `runs/RUN_ID/visualization/FIGURE_ID/` 内的图片、绘图数据和 manifest；manifest 包含 `schema_version: 1`、`figure_id`、`image_file`、`data_file`、`evaluation_ids`，至少引用一条已结束测量。所有文件引用相对运行目录。

## 方法

1. 从正式结果提取数值，标注指标、方向、数据用途、候选、样本量和评测 ID；任务或评测协议不同的结果分别展示。
2. 显示无效解、超时、失败与缺失，缺失数值保留为空，不以删除失败样本美化比较。
3. 明确研究数据与最终数据分别支持的结论，candidate feedback 筛选的 challenge 标出选择机制及其限制。
4. 保存绘图数据及来源引用，使每个比较可追溯；只绘制已有授权结果，不能为图表补测或修改本次研究。

## 调用与完成条件

以 `deliver RUN_ID figure visualization/FIGURE_ID/manifest.json --module visualization` 登记，工具调用 `validate.py` 检查图片/数据引用与 evaluation_ids。成功返回 manifest、图表所支持的比较及限制；没有可用测量或来源不完整时说明原因并省略图，不制造空白成绩。


执行与授权、路径、冻结交付、原生参数及预算边界遵守主 [AGENTS.md](../../../AGENTS.md)；具体交付或程序调用以上述本模块规则为准。
