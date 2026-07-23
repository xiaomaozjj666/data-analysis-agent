"""FastAPI 路由模块包。

各路由模块（``sessions`` / ``analysis`` / ``artifacts`` / ``settings``）通过
``APIRouter`` 定义端点，由 ``data_agent.api`` 统一 ``include_router`` 装配。
路由层通过 ``data_agent.api`` 访问共享单例（``api.registry``、
``api.bootstrap_settings`` 等）以便测试 monkeypatch 生效；纯辅助函数与常量
直接从 ``data_agent.registry`` 导入。
"""
