"""端到端场景演示：复现“只累计学时误判合格”的问题与新系统的修正。

运行：python3 tools/demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_backend.domain import aggregates as agg
from evidence_backend.event_store import EventStore
from evidence_backend.identity import Principal
from evidence_backend.repository import AggregateRepo
from evidence_backend.services import (
    AppealService,
    EvaluationService,
    EvidenceService,
    QueryService,
    RegistryService,
)

rv = Principal("rv1", agg.ROLE_REVIEWER, "")
teacher = Principal("T001", agg.ROLE_TEACHER, "")


def show(title: str, payload: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    store = EventStore(":memory:")
    repo = AggregateRepo(store)
    reg = RegistryService(store, repo)
    evd = EvidenceService(store, repo, reg)
    eva = EvaluationService(store, repo, reg)
    apl = AppealService(store, repo, eva)
    q = QueryService(store, repo)

    # 1) 能力目标与三类培训单元
    for gid, title in [("G1", "数字化教学设计"), ("G2", "企业实践转化"), ("G3", "协同教研反思")]:
        reg.register_goal(rv, goal_id=gid, title=title)
    u_online = reg.register_training_unit(rv, code="ONLINE-1", title="线上研修课",
        category="线上研修", goal_id="G1", default_hours=10)["unit_id"]
    u_ent = reg.register_training_unit(rv, code="ENT-1", title="企业跟岗",
        category="企业实践", goal_id="G2", default_hours=16)["unit_id"]
    u_jy = reg.register_training_unit(rv, code="JY-1", title="联合教研",
        category="联合教研", goal_id="G3", default_hours=12)["unit_id"]
    iss = reg.register_issuer(rv, name="研修平台", kind="平台", org_id="ORG_A")["issuer_id"]
    reg.register_teacher(rv, teacher_id="T001", name="张老师")

    # 2) 适用规则版本：G1 不仅要 20 学时，还需至少 2 份证据；联合教研可部分折算替代 G1。
    rs = reg.register_ruleset(
        rv, name="教师资格规则", version="2026.1",
        goals=[
            {"goal_id": "G1", "title": "数字化教学设计", "required_hours": 20, "required_evidence_count": 2},
            {"goal_id": "G2", "title": "企业实践转化", "required_hours": 16, "required_evidence_count": 1},
            {"goal_id": "G3", "title": "协同教研反思", "required_hours": 12, "required_evidence_count": 1},
        ],
        substitutions=[{"unit_id": u_jy, "goal_id": "G1", "ratio": 0.5, "max_hours": 6}],
    )
    reg.publish_ruleset(rv, rs["ruleset_id"], "2026-01-01")

    # 3) 教师提交三类证明（合计 40 学时——旧系统只看学时会判合格）
    submitted = [
        evd.submit(teacher, teacher_id="T001", unit_id=u_online, issuer_id=iss,
                   hours=12, issued_on="2026-03-01", external_ref="ONLINE-1"),
        evd.submit(teacher, teacher_id="T001", unit_id=u_ent, issuer_id=iss,
                   hours=16, issued_on="2026-05-01", external_ref="ENT-1"),
        evd.submit(teacher, teacher_id="T001", unit_id=u_jy, issuer_id=iss,
                   hours=12, issued_on="2026-06-01", external_ref="JY-1"),
    ]
    # 线上研修重复提交一次：只追加重复关联，不重复计学时
    evd.submit(teacher, teacher_id="T001", unit_id=u_online, issuer_id=iss,
               hours=12, issued_on="2026-03-01", external_ref="ONLINE-1")

    for e in submitted:
        evd.verify(rv, e["evidence_id"], decision="ACCEPT")

    total = sum(e["hours"] for e in submitted)
    print(f"旧逻辑：只累计学时 {total:g} >= 40 → 误判“合格”，但关键能力目标是否覆盖无人核查。")

    # 4) 新判定：按能力目标核算，给出缺口与每份证据的贡献
    report = eva.evaluate(rv, "T001")
    show("新逻辑判定：仍不合格，缺口与贡献可解释", {
        "result": report["result"],
        "summary": report["explanation"]["summary"],
        "gaps": report["gaps"],
        "g1_evidence_roles": [
            {"evidence_id": c["evidence_id"], "path": c["path"],
             "counted_hours": c["counted_hours"], "necessity": c["necessity"]}
            for g in report["goals"] if g["goal_id"] == "G1" for c in g["contributions"]
        ],
    })

    # 5) 补一份线上研修证据 → 合格；随后撤销企业实践证明 → 自动重判暴露 G2 缺口
    extra = evd.submit(teacher, teacher_id="T001", unit_id=u_online, issuer_id=iss,
                       hours=10, issued_on="2026-07-01", external_ref="ONLINE-2")
    evd.verify(rv, extra["evidence_id"], decision="ACCEPT")
    qualified = eva.evaluate(rv, "T001")
    print(f"补足证据后：{qualified['result']}")

    evd.revoke(rv, submitted[1]["evidence_id"], reason="签发方发现代签，撤销证明")
    re_eval = eva.evaluate(rv, "T001", trigger=agg.TRIGGER_REVOCATION, reason="撤销后重判")
    show("撤销后重判（旧判定仅被标记 SUPERSEDED，历史保留）", {
        "result": re_eval["result"],
        "gaps": re_eval["gaps"],
        "revoked_basis": [b for b in re_eval["evidence_basis"] if not b["counted"]],
        "evaluation_chain": [
            {"evaluation_id": h["evaluation_id"], "status": h["status"], "trigger": h["trigger"]}
            for h in q.evaluation_history(rv, "T001")
        ],
    })

    # 6) 申诉复核：恢复证据并撤销原判定，形成新判定
    appeal = apl.open_appeal(teacher, teacher_id="T001", reason="企业实践真实，代签系误认")
    evd.reinstate(rv, submitted[1]["evidence_id"], note="复核确认真实")
    apl.review_appeal(rv, appeal["appeal_id"], decision=agg.APPEAL_OVERTURNED,
                      note="撤销依据不成立")
    final = q.current_evaluation(teacher, "T001")
    show("申诉复核后的最终判定", {
        "result": final["result"], "trigger": final["trigger"],
        "appeal_status": q.get_appeal(teacher, appeal["appeal_id"])["status"],
        "summary": final["explanation"]["summary"],
    })


if __name__ == "__main__":
    main()
