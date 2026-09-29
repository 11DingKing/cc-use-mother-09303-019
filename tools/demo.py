"""命令行演示：在内存库中跑完整场景并打印可追溯判定报告。

用法：
    python3 tools/demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfolio_backend.database import Database
from portfolio_backend.seed import seed
from portfolio_backend.services import Principal, Service
from portfolio_backend.domain import Role


def _render_evaluation(title: str, data: dict) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(f"规则版本：{data['rule_set']}@v{data['rule_version']}")
    print(f"结论：{data['decision']}　{data['summary']}")
    print()
    print("能力目标覆盖与缺口：")
    for g in data["goals"]:
        mark = "✔" if g["satisfied"] else "✘"
        print(
            f"  {mark} {g['goal_code']} {g['goal_name']}："
            f"{g['total_hours']:g}/{g['required_hours']:g} 学时"
            f"（直接 {g['direct_hours']:g}、替代 {g['substitution_hours']:g}、"
            f"申诉 {g['appeal_hours']:g}）"
        )
        for c in g["contributions"]:
            cap = "〔截顶〕" if c["capped"] else ""
            print(f"      ← 证据 {c['evidence_id']} {c['kind']} +{c['hours']:g} 学时{cap}")
        if g["gap_hours"] > 0:
            print(f"      ⚠ 缺口 {g['gap_hours']:g} 学时")
    print()
    print("每份证据对结论的实际贡献：")
    for e in data["evidence"]:
        points = "、".join(e["decision_points"]) or "—"
        print(
            f"  · {e['evidence_id']} [{e['status']}] {e['unit_name']} "
            f"{e['hours']:g} 学时（签发方 {e['issuer_id']}）→ 作用于目标：{points}"
        )
        if e["excluded_reason"]:
            print(f"      未计入原因：{e['excluded_reason']}")
        for c in e["contributions"]:
            cap = "〔截顶〕" if c["capped"] else ""
            print(f"      → {c['goal_code']} {c['kind']} +{c['hours']:g}{cap}：{c['note']}")
    print()
    if data["gaps"]:
        print("缺口说明：")
        for gap in data["gaps"]:
            print(f"  - {gap}")
        print()


def main() -> None:
    db = Database(":memory:")
    result = seed(db)
    svc = Service(db)

    _render_evaluation(
        "场景一：王老师——三类证明学时充足，但关键能力目标零覆盖",
        result["wang"]["evaluation"],
    )
    _render_evaluation(
        "场景二：李老师（首次判定）——部分替代被封顶，G3 缺 8 学时",
        result["li"]["first_evaluation"],
    )
    _render_evaluation(
        "场景二：李老师（申诉复核后）——追加认定 8 学时，合格",
        result["li"]["second_evaluation"],
    )

    print("=" * 72)
    print("可追溯性检查")
    print("=" * 72)
    reviewer = svc.principal("r_chen")
    history = svc.list_evaluations(reviewer, "t_li")
    print(f"李老师历史判定记录数：{len(history)}（旧结论保留，不被覆盖）")
    chain = db.verify_chain()
    print(f"只追加事件散列链：{json.dumps(chain, ensure_ascii=False)}")
    events = svc.list_events(Principal("u_admin", Role.ADMIN))
    print(f"事件总数：{len(events)}；撤销/重复/申诉/复核均为追加事件")
    db.close()


if __name__ == "__main__":
    main()
