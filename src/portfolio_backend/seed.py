"""可复现的演示数据。

场景原型即需求中描述的问题：

- 教师 t_wang 提交线上研修（online）、企业实践（enterprise）、
  联合教研（joint_research）三类证明，学时相加远超门槛；
- 但 v1 规则把企业实践单元错误地映射到了非关键目标，
  关键能力目标 G3「岗位实践指导」没有任何证据覆盖——
  旧系统按累计学时判合格，本系统判定不合格并给出缺口与逐证据贡献；
- 规则发布后不可修改，只能发布 v2 修正映射（适用规则版本可追溯）；
- 同时演示：重复提交去重、签发方撤销、部分替代截顶、
  教师申诉、机构审核员复核追加认定。

另建教师 t_li 走完全部正向流程（含部分替代与申诉成立后合格）。
"""
from __future__ import annotations

from portfolio_backend.domain import Role, iso, utcnow
from portfolio_backend.services import Principal, Service
from portfolio_backend.database import Database


def seed(db: Database) -> dict:
    svc = Service(db)
    admin = Principal("u_admin", Role.ADMIN, name="平台管理员")

    # 自举首位平台管理员：写入用户表并记录事件（之后可直接经接口鉴权）。
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, role, created_at) VALUES (?, ?, ?, ?)",
            ("u_admin", "平台管理员", Role.ADMIN.value, iso(utcnow())),
        )
        db.append_event(
            conn,
            event_type="user.created",
            payload={"user_id": "u_admin", "name": "平台管理员",
                     "role": Role.ADMIN.value, "org_id": None, "bootstrap": True},
            actor_id="u_admin",
        )

    # ---- 机构 ----------------------------------------------------------
    svc.create_organization(admin, "org_school", "市教师发展中心", "培训机构")
    svc.create_organization(admin, "org_school2", "第二教师发展中心", "培训机构")
    svc.create_organization(admin, "iss_univ", "师范大学网络学院", "签发方")
    svc.create_organization(admin, "iss_enterprise", "合作企业实训基地", "签发方")
    svc.create_organization(admin, "iss_research", "学科教研联合体", "签发方")

    # ---- 用户 ----------------------------------------------------------
    svc.create_user(admin, "t_wang", "王老师", Role.TEACHER)
    svc.create_user(admin, "t_li", "李老师", Role.TEACHER)
    svc.create_user(admin, "r_chen", "陈审核员", Role.ORG_REVIEWER, org_id="org_school")
    svc.create_user(admin, "r_zhao", "赵审核员", Role.ORG_REVIEWER, org_id="org_school2")
    svc.create_user(admin, "s_univ", "大学经办人", Role.ISSUER_STAFF, org_id="iss_univ")
    svc.create_user(admin, "s_ent", "企业经办人", Role.ISSUER_STAFF, org_id="iss_enterprise")
    svc.create_user(admin, "s_res", "教研经办人", Role.ISSUER_STAFF, org_id="iss_research")

    # ---- 培训单元：线上研修 / 企业实践 / 联合教研 -----------------------
    svc.register_unit(admin, "online_study", "线上研修课程", "线上研修")
    svc.register_unit(admin, "enterprise_practice", "企业跟岗实践", "企业实践")
    svc.register_unit(admin, "joint_research", "校际联合教研", "联合教研")
    svc.register_unit(admin, "guest_lecture", "企业专家讲座", "企业实践")

    for issuer, unit in [
        ("iss_univ", "online_study"),
        ("iss_enterprise", "enterprise_practice"),
        ("iss_enterprise", "guest_lecture"),
        ("iss_research", "joint_research"),
    ]:
        svc.authorize_issuer(admin, issuer, unit)

    # ---- 规则 v1（发布后冻结；含“关键目标无人覆盖”的缺陷） -------------
    svc.create_rule_set(admin, "standard", "教师资格研修标准")
    svc.create_rule_version(admin, "standard", note="v1：初版，企业实践映射有误")
    v1 = 1
    svc.add_goal(admin, "standard", v1, "G1", "课程理论更新", 40, sort_order=1)
    svc.add_goal(admin, "standard", v1, "G2", "教研协作能力", 24, sort_order=2)
    svc.add_goal(admin, "standard", v1, "G3", "岗位实践指导", 32, sort_order=3)
    svc.map_unit_goal(admin, "standard", v1, "online_study", "G1")
    svc.map_unit_goal(admin, "standard", v1, "joint_research", "G2")
    # v1 缺陷：enterprise_practice 错挂到 G1，G3 没有任何单元覆盖
    svc.map_unit_goal(admin, "standard", v1, "enterprise_practice", "G1", weight=0.5)
    # 专家讲座可部分替代联合教研：折算 50%，封顶为 G2 要求的 25%（=6学时）
    svc.add_substitution(
        admin, "standard", v1, "guest_lecture", "joint_research",
        ratio=0.5, cap_ratio=0.25,
    )
    svc.publish_rule_version(admin, "standard", v1)

    # ---- 规则 v2：修正企业实践 → G3（新版本，旧档案仍可追溯 v1） --------
    svc.create_rule_version(admin, "standard", note="v2：企业实践正确映射到岗位实践指导")
    v2 = 2
    svc.add_goal(admin, "standard", v2, "G1", "课程理论更新", 40, sort_order=1)
    svc.add_goal(admin, "standard", v2, "G2", "教研协作能力", 24, sort_order=2)
    svc.add_goal(admin, "standard", v2, "G3", "岗位实践指导", 32, sort_order=3)
    svc.map_unit_goal(admin, "standard", v2, "online_study", "G1")
    svc.map_unit_goal(admin, "standard", v2, "joint_research", "G2")
    svc.map_unit_goal(admin, "standard", v2, "enterprise_practice", "G3")
    # 讲座在 v2 中也只作替代单元，不直连任何目标
    svc.add_substitution(
        admin, "standard", v2, "guest_lecture", "joint_research",
        ratio=0.5, cap_ratio=0.25,
    )
    svc.publish_rule_version(admin, "standard", v2)

    # ---- 建档：王老师按 v1（保留缺陷场景），李老师按 v2 ------------------
    svc.enroll_teacher(admin, "t_wang", "org_school", "standard", v1)
    svc.enroll_teacher(admin, "t_li", "org_school", "standard", v2)

    wang = svc.principal("t_wang")
    li = svc.principal("t_li")
    univ = svc.principal("s_univ")
    ent = svc.principal("s_ent")
    res = svc.principal("s_res")
    reviewer = svc.principal("r_chen")

    # ---- 王老师：三类证明（v1 下总学时 96 远超门槛，但 G3 零覆盖） ------
    ev_online = svc.submit_evidence(
        wang, "t_wang", "online_study", "iss_univ", 48, "2026-03-15", "UNIV-2026-0088"
    )["evidence_id"]
    ev_ent = svc.submit_evidence(
        wang, "t_wang", "enterprise_practice", "iss_enterprise", 24, "2026-04-20", "ENT-2026-0107"
    )["evidence_id"]
    ev_joint = svc.submit_evidence(
        wang, "t_wang", "joint_research", "iss_research", 24, "2026-05-11", "RES-2026-0331"
    )["evidence_id"]

    # 同一份企业实践证明重复提交 → 登记“重复提交”，不重复计入
    dup = svc.submit_evidence(
        wang, "t_wang", "enterprise_practice", "iss_enterprise", 24, "2026-04-20", "ENT-2026-0107"
    )

    svc.verify_evidence(univ, ev_online)
    svc.verify_evidence(ent, ev_ent)
    svc.verify_evidence(res, ev_joint)

    # ---- 李老师：v2 下完整流程，含撤销、部分替代与申诉 ------------------
    li_online = svc.submit_evidence(
        li, "t_li", "online_study", "iss_univ", 40, "2026-03-10", "UNIV-2026-0071"
    )["evidence_id"]
    li_lecture_bad = svc.submit_evidence(
        li, "t_li", "guest_lecture", "iss_enterprise", 16, "2026-04-02", "ENT-2026-0099"
    )["evidence_id"]
    svc.verify_evidence(univ, li_online)
    svc.verify_evidence(ent, li_lecture_bad)
    # 原讲座证明被签发方撤销（追加撤销历史，记录保留）
    svc.revoke_evidence(ent, li_lecture_bad, reason="证明编号录入有误，应企业方要求撤销")

    # 重新提交两份讲座：折算后本应为 4+12=16 学时，但 G2 替代封顶 6 学时，
    # 第一份计入 4（未截顶），第二份只剩 2 学时预算（截顶）→ 部分替代。
    li_lecture_a = svc.submit_evidence(
        li, "t_li", "guest_lecture", "iss_enterprise", 8, "2026-04-08", "ENT-2026-0120"
    )["evidence_id"]
    li_lecture_b = svc.submit_evidence(
        li, "t_li", "guest_lecture", "iss_enterprise", 24, "2026-05-09", "ENT-2026-0145"
    )["evidence_id"]
    svc.verify_evidence(ent, li_lecture_a)
    svc.verify_evidence(ent, li_lecture_b)

    # 联合教研 18 学时直连 G2（要求 24），加替代封顶的 6 学时恰好达标
    li_joint = svc.submit_evidence(
        li, "t_li", "joint_research", "iss_research", 18, "2026-05-15", "RES-2026-0402"
    )["evidence_id"]
    svc.verify_evidence(res, li_joint)

    # 企业实践 24 学时直连 G3（要求 32）→ G3 尚缺 8
    li_practice = svc.submit_evidence(
        li, "t_li", "enterprise_practice", "iss_enterprise", 24, "2026-05-20", "ENT-2026-0160"
    )["evidence_id"]
    svc.verify_evidence(ent, li_practice)

    # 首次判定：李老师 G3 缺口 8 学时（讲座对 G3 的直连权重 0.25 也计入）
    first_eval = svc.evaluate_teacher(reviewer, "t_li")

    # 李老师申诉：另有 8 学时跟岗实践写在撤销重开的旧证明附件里
    appeal = svc.file_appeal(
        li, "t_li",
        reason="企业实践另有 8 学时带教任务已由企业确认，请求复核认定至 G3",
        evidence_id=li_practice,
    )["appeal_id"]
    svc.review_appeal(
        reviewer, appeal, uphold=True,
        decision_note="企业方补充确认函属实，追加认定 G3 8 学时",
        credits=[{"evidence_id": li_practice, "goal_code": "G3", "hours": 8,
                  "reason": "补认带教任务 8 学时"}],
    )

    # 复核后重新判定 → 合格
    second_eval = svc.evaluate_teacher(reviewer, "t_li")

    # 王老师按 v1 判定：学时 96 仍不合格，G3 缺口 32
    wang_eval = svc.evaluate_teacher(reviewer, "t_wang")

    return {
        "wang": {"teacher_id": "t_wang", "evaluation": wang_eval,
                 "evidences": {"online": ev_online, "enterprise": ev_ent,
                               "joint": ev_joint, "duplicate": dup["evidence_id"]}},
        "li": {"teacher_id": "t_li",
               "first_evaluation": first_eval, "second_evaluation": second_eval,
               "appeal_id": appeal,
               "evidences": {"online": li_online, "revoked": li_lecture_bad,
                             "lecture_a": li_lecture_a, "lecture_b": li_lecture_b,
                             "joint": li_joint, "practice": li_practice}},
        "rules": {"rule_set": "standard", "v1": v1, "v2": v2},
    }
