# 教师培训证据组合

面向教师发展部门的培训证据登记、验证、组合与资格解释后端。系统不按“累计学时达标”判定，而是按**能力目标逐项核算**每份证据的实际贡献，给出通过与否、能力缺口与逐证据去向解释；撤销、重复提交、部分替代、申诉复核全部以**只追加事件**留痕，机构间资料按权限隔离，规则按**发布即冻结的版本**适用。

## 领域契约

`domain/contract.json` 定义角色（参训教师、培训机构/资格审核员、签发方、平台管理员）、状态（提交 → 验证 → 组合 → 判定 → 申诉）与四个不变量：

- **证据组合**：为每位教师重建可追溯证据组合（注册信息、证据清单、完整事件历史）。
- **签发方验证**：证明须由被授权该培训单元的签发方验证后才计入；签发方可驳回、撤销。
- **替代规则**：替代关系按比例折算，并按目标设置封顶比例——证据超额部分被截顶（部分替代），截顶在贡献明细中可见。
- **资格解释**：判定结果包含逐目标缺口（直接/替代/申诉三类学时）与每份证据对结论的实际贡献。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/portfolio_backend/`：
  - `domain.py`：枚举、值对象（贡献、目标结果、判定报告）、错误类型。
  - `engine.py`：资格判定引擎（**纯函数**，不依赖数据库）。
  - `database.py`：SQLite schema；只追加事件表（UPDATE/DELETE 触发器 + SHA-256 散列链）。
  - `services.py`：用例编排与四类角色的权限强制。
  - `seed.py`：可复现演示数据（学时充足但关键目标零覆盖、重复、撤销、截顶、申诉全流程）。
  - `api.py`：标准库 `http.server` JSON 接口（无第三方依赖）。
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo.py`：命令行端到端场景报告。
- `tests/`：契约回归 + 引擎单测 + 服务集成测试 + HTTP 端到端测试。

## 验证

```bash
# 全部测试（含原有契约测试，共 40+ 用例）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约命令行检查
python3 tools/check_contract.py domain/contract.json

# 端到端场景演示（无需起服务）
python3 tools/demo.py
```

## 启动服务

```bash
PYTHONPATH=src python3 -m portfolio_backend.api --db portfolio.sqlite3 --port 8080 --seed
```

所有请求以 `X-User-Id` 头标识操作者（种子用户：`u_admin` 平台管理员、`t_wang`/`t_li` 教师、`r_chen`/`r_zhao` 两机构审核员、`s_univ`/`s_ent`/`s_res` 三个签发方经办人）。

## HTTP 接口

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/admin/organizations` `/admin/users` `/admin/units` | 管理员 | 机构、人员、培训单元登记 |
| POST | `/admin/issuer-authorizations` | 管理员 | 授权签发方可签发的单元 |
| POST | `/admin/rule-sets` | 管理员 | 新建规则集 |
| POST | `/admin/rule-sets/{rs}/versions` | 管理员 | 新建草稿版本 |
| POST | `.../versions/{v}/goals` `/unit-goals` `/substitutions` | 管理员 | 草稿中登记能力目标、单元—目标映射、替代关系 |
| POST | `.../versions/{v}/publish` | 管理员 | 发布即冻结（至少含一个目标） |
| GET | `/rule-sets/{rs}/versions/{v}` | 任意 | 查看冻结版本内容 |
| POST | `/admin/enrollments` | 管理员 | 教师建档并绑定已发布规则版本 |
| POST | `/teachers/{id}/evidences` | 教师本人/本机构审核员 | 提交证明（指纹去重，重复也留痕） |
| POST | `/evidences/{id}/verify\|reject\|revoke` | 授权签发方 | 验证、驳回、撤销（追加历史） |
| GET | `/teachers/{id}/portfolio` | 本人/本机构/管理员 | 可追溯证据组合与完整历史 |
| POST/GET | `/teachers/{id}/evaluations` | 本机构审核员等 | 判定（返回缺口与逐证据贡献）/ 历史判定 |
| POST | `/teachers/{id}/appeals` | 教师本人 | 提起申诉 |
| POST | `/appeals/{id}/review` | 本机构审核员 | 复核；成立时 `credits` 追加目标学时认定 |
| GET | `/reviewer/teachers` `/issuer/evidences` | 对应角色 | 机构视角名册与待办 |
| GET | `/events` | 按角色过滤 | 事件日志（教师限本人、审核员限本机构、签发方限本机构证据） |
| GET | `/audit/chain` | 管理员 | 重算事件散列链，检测日志是否被篡改 |

## 关键设计

### 判定不是学时加总

引擎对教师绑定的冻结规则版本逐目标核算：

```
目标满足 ⇔ 直接覆盖学时 + 替代覆盖学时（折算并截顶）+ 申诉认定学时 ≥ 目标要求学时
资格合格 ⇔ 所有能力目标均满足
```

验证不通过、已撤销、待验证、重复提交的证据一律排除并给出原因；培训单元未映射到任何目标时，其学时**不计入**任何目标——这正是“学时够但关键能力目标无证据”被暴露的位置。

### 规则版本化

规则版本为 `draft → published`：草稿可改，发布后目标、映射、替代关系冻结；修正只能发新版本（v1→v2），教师档案记录其适用版本，旧判定永远按旧版本可复现。

### 只追加历史

撤销、驳回、重复登记、每次判定、申诉提交与复核结论都追加到 `events` 表；该表由触发器禁止 UPDATE/DELETE，且事件经 `prev_hash` 串成 SHA-256 链，`/audit/chain` 可审计。当前状态行只是事件流的物化结果。

### 权限隔离

- 教师：仅本人资料与申诉。
- 机构审核员：仅本机构在册教师；外机构访问返回 403。
- 签发方：仅本机构签发的证明，且须持有该单元授权才能验证。
- 管理员：全局登记与审计，不代行机构判定。
