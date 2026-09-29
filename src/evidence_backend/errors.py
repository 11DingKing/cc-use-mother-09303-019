"""领域与应用层错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "DOMAIN_ERROR"
    http_status = 400


class ValidationError(DomainError):
    """登记或命令参数不满足领域约束。"""

    code = "VALIDATION_ERROR"
    http_status = 400


class NotFound(DomainError):
    """引用的聚合或资源不存在。"""

    code = "NOT_FOUND"
    http_status = 404


class PermissionDenied(DomainError):
    """角色或机构权限不足（机构间隔离）。"""

    code = "PERMISSION_DENIED"
    http_status = 403


class Conflict(DomainError):
    """聚合状态或版本冲突（重复登记、非法状态迁移）。"""

    code = "CONFLICT"
    http_status = 409


class AuthenticationError(DomainError):
    """缺少身份凭证或凭证无效。"""

    code = "UNAUTHENTICATED"
    http_status = 401
