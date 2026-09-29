"""教师培训证据组合后端。

模块划分：

- ``domain``：领域值对象、状态枚举与错误类型。
- ``engine``：资格判定引擎（纯函数，不依赖数据库）。
- ``database``：SQLite schema 与只追加事件日志。
- ``services``：应用服务（用例编排、权限校验、历史追加）。
- ``api``：基于标准库 http.server 的 JSON HTTP 接口。
- ``seed``：可复现的演示数据（含“只累计学时却漏覆盖能力目标”的场景）。
"""

from .database import Database
from .engine import evaluate
from .services import Principal, Service

__all__ = ["Database", "Principal", "Service", "evaluate"]
