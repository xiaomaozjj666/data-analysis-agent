"""ECharts 渲染子包：按职责拆分的 option 生成、密度视图、解读与输出层。

模块划分（依赖方向自上而下，无环）：

- ``primitives``: 中性层（颜色/数值/分箱/数值轴），只依赖常量与 ``tools/_helpers``
- ``interpret``: 自动白话解读（含密度视图解读）
- ``series_cartesian`` / ``series_scatter`` / ``series_distribution`` /
  ``series_3d`` / ``series_matrix`` / ``series_hierarchy``: 各图表族 option 构建
- ``density_view`` / ``density_option``: 大数据散点的密度视图与 option 装配
- ``output``: option 序列化与 standalone HTML 模板渲染
- ``dispatch``: chart_type 分派与 ``_render_echarts`` 编排（写产物）

对外入口仍为 ``data_agent.echarts_engine``（保留原导入路径的门面模块）。
"""
