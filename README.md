# 教师培训证据组合后端

面向教师发展部门的资格判定后端。系统不再以“累计学时达标”作为合格依据，而是登记**培训单元、证据签发方、能力目标、替代关系与适用规则版本**，为每位教师形成可追溯的证据组合，按能力目标给出通过/不通过、**缺口解释**以及**每份证据对结论的实际贡献**。

仅依赖 Python 3.11 标准库（SQLite + `http.server`），无第三方依赖。

## 核心设计

- **事件溯源 + 只追加日志**：提交、验证、撤销、重复关联、部分替代、申诉复核全部以事件追加到 `event_log`；SQLite 触发器在数据库层物理禁止 `UPDATE/DELETE`。
- **规则版本化**：规则集（DRAFT→PUBLISHED→DEPRECATED）带生效日期，判定记录固化所用规则版本；规则变更后旧判定仍可按当时版本解释，新判定自动选取适用版本。
- **能力目标判定引擎**（`domain/engine.py`，纯函数、确定性）：
  - 按目标汇总**直接证据**与**替代证据**（折算比例 `ratio`、替代学时上限 `max_hours`）；
  - 同时校验学时与最少证据份数，单一证明无法仅凭学时凑数；
  - 输出每份证据的 `DIRECT/SUBSTITUTION` 路径、计入学时、`NEEDED/SURPLUS` 必要性；
  - 待验证/重复/拒绝/撤销的证据一律不计入，并给出排除原因。
- **机构数据隔离**：教师只见本人资料；签发机构只见本机构证明，且只能验证本机构证明；审核员全域可见；原始事件流仅审核员可读。
- **撤销后重判与申诉链**：撤销证据后重新判定，旧判定标记 `SUPERSEDED` 但永不删除；申诉复核可维持（UPHELD）或撤销（OVERTURNED）原判定并生成链接前序判定的新判定。

## 目录

- `domain/contract.json`：领域角色、状态、约束与样例（提交→验证→组合→判定→申诉）。
- `src/evidence_backend/`
  - `event_store.py`：只追加事件存储、快照与读模型（含禁改禁删触发器）。
  - `domain/aggregates.py`：聚合 reducer，从事件流重建状态。
  - `domain/engine.py`：能力覆盖、替代折算、缺口与贡献解释引擎。
  - `identity.py`：账户、Bearer 令牌与角色/机构主体。
  - `repository.py`：乐观并发的聚合仓储。
  - `services.py`：登记、证据流转、判定、申诉、隔离查询应用服务。
  - `http.py`：JSON HTTP API 与服务启动入口。
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo.py`：端到端场景演示（复现“只累计学时误判合格”及修正）。
- `tests/`：契约回归、引擎规则、服务层与真实 HTTP 端到端测试。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 场景演示
python3 tools/demo.py

# 启动 HTTP 服务
python3 -m evidence_backend.http --db evidence.db --host 127.0.0.1 --port 8080
```

（运行服务/演示时将 `src` 加入 PYTHONPATH，例如 `PYTHONPATH=src python3 -m evidence_backend.http`。）

## API 摘要

所有接口前缀 `/api`，除 `POST /auth/token` 外均需 `Authorization: Bearer <token>`。

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| POST `/auth/token` | - | 账户换令牌 |
| POST `/admin/accounts` | admin/reviewer | 创建角色账户（可带 `org_id`） |
| POST `/admin/issuers`、`/admin/goals`、`/admin/teachers` | 审核员 | 登记签发方、能力目标、教师 |
| POST `/admin/issuers/{id}/status` | 审核员 | 签发方受信/暂停（暂停后新证明不受理） |
| POST `/admin/training-units`（`/{id}/retire`） | 审核员 | 登记/停用培训单元（类别限线上研修、企业实践、联合教研） |
| POST `/admin/rulesets`（`/{id}/publish`、`/{id}/deprecate`） | 审核员 | 规则版本登记、发布、废止 |
| GET `/rulesets/applicable?as_of=YYYY-MM-DD` | 已认证 | 查询当日适用规则版本 |
| POST `/evidences` | 教师（本人） | 提交证明；同外部凭证号或同签发信息自动关联为重复件 |
| POST `/evidences/{id}/verify` | 审核员/所属签发机构 | ACCEPT（可核定时数）/REJECT（需理由） |
| POST `/evidences/{id}/revoke`、`/reinstate` | 签发机构或审核员 / 审核员 | 撤销证明（需原因）、撤销后恢复 |
| GET `/evidences`、`/evidences/{id}`、`/evidences/{id}/history` | 按隔离 | 证据清单、详情、只追加历史 |
| POST `/evaluations` | 审核员 | 按适用（或指定已发布）规则版本判定 |
| GET `/teachers/{id}/portfolio` | 本人/审核员 | 证据组合 + 当前判定 |
| GET `/teachers/{id}/evaluations` | 本人/审核员 | 历次判定链（含 SUPERSEDED） |
| POST `/appeals`、`/appeals/{id}/review` | 教师 / 审核员 | 申诉、复核（UPHELD/OVERTURNED，后者生成新判定） |
| GET `/history` | 审核员 | 原始事件流审计 |

判定结果示例（节选）：

```json
{
  "result": "NOT_QUALIFIED",
  "ruleset_version": "教师资格规则@2026.1",
  "gaps": [{"goal_id": "G2", "gap_hours": 16, "reasons": ["能力目标尚缺 16 学时的有效证据", "至少需要 1 份直接或替代证据覆盖该目标"]}],
  "goals": [{"goal_id": "G1", "covered_hours": 18, "contributions": [
    {"evidence_id": "ev_1", "path": "DIRECT", "counted_hours": 12, "necessity": "NEEDED"},
    {"evidence_id": "ev_3", "path": "SUBSTITUTION", "substitution_id": "sub_jy_g1", "ratio": 0.5, "counted_hours": 6, "necessity": "NEEDED"}
  ]}],
  "evidence_basis": [{"evidence_id": "ev_2", "status": "REVOKED", "counted": false, "exclusion_reason": "证明已被签发方撤销，不再作为判定依据"}]
}
```
