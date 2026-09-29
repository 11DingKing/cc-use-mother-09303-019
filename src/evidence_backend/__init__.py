"""教师培训证据组合后端。

零第三方依赖：事件溯源存储、能力目标判定引擎、机构隔离查询与 HTTP API。

子模块：
- ``event_store``：只追加事件存储；
- ``domain.engine``：能力覆盖与替代折算判定引擎；
- ``identity``：账户与机构主体；
- ``services``：应用服务层；
- ``http``：JSON HTTP API（``python -m evidence_backend.http``）。
"""
from .errors import (
    AuthenticationError,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    ValidationError,
)

__all__ = [
    "DomainError",
    "ValidationError",
    "NotFound",
    "PermissionDenied",
    "Conflict",
    "AuthenticationError",
]
