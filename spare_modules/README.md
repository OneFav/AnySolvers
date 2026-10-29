# 备用方法

按 `spare_modules/<类别>/<方法>/` 存放完整方法包。当前代码不能直接绑定或执行这里的包。启用时先移至 modules，再更新配置并检查接口；停用方法在不被活动运行或共享初始化依赖后整包移回。

当前备用：`datasets/feedback_challenges`——根据开发反馈构造困难实例，只产出 challenge，不能作为独立最终测试集生成器。

详见 [模块管理说明](../docs/MODULES.md)。
