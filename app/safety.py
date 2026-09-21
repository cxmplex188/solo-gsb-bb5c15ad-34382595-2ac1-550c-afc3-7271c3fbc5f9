"""加液步骤安全确认服务。

每个加液步骤可勾选防护措施（护目镜 / 通风 / 手套等）并填写安全备注，
保存正式推演（persist=true）前必须完成全部步骤的确认：

- confirmed    已确认：措施与备注已由使用者确认
- stale        待复核：步骤内容或其试剂自确认后发生变更，确认被自动清除
- unconfirmed  未确认：从未做过确认

确认以 (方案, 步骤序号) 为稳定键，通过步骤内容指纹判断变更：
步骤整体替换时，同序号且指纹一致的确认保留，其余全部置为待复核；
试剂档案被修改时，引用该试剂的全部步骤确认一并失效。
预检（simulate-dry / persist=false）不受此门槛限制。
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from . import models

CONFIRMED = "confirmed"
STALE = "stale"
UNCONFIRMED = "unconfirmed"

STATUS_LABELS = {
    CONFIRMED: "已确认",
    STALE: "待复核",
    UNCONFIRMED: "未确认",
}

# 可勾选的防护措施目录（前端通过 /api/plans/safety-measures 获取）
SAFETY_MEASURES = [
    {"key": "goggles", "label": "护目镜", "description": "佩戴防溅护目镜/面屏"},
    {"key": "fume_hood", "label": "通风", "description": "在通风橱/通风良好处操作"},
    {"key": "gloves", "label": "手套", "description": "佩戴合适材质的防护手套"},
    {"key": "lab_coat", "label": "实验服", "description": "穿实验服，束扎长发"},
]
MEASURE_KEYS = {m["key"] for m in SAFETY_MEASURES}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def step_categories(db: Session, step: models.Step) -> list[str]:
    """步骤关联试剂的类别：优先试剂档案，兜底步骤创建时的快照。"""
    if step.reagent_id is not None:
        reagent = db.get(models.Reagent, step.reagent_id)
        if reagent is not None:
            return json.loads(reagent.categories or "[]")
    return json.loads(step.categories or "[]")


def step_fingerprint(db: Session, step: models.Step) -> str:
    """步骤"影响安全确认"的内容指纹。

    包含试剂身份与类别、用量、时刻、备注；不含 order_index 本身——
    步骤被重排（位置变化）时指纹自然对不上同序号的旧确认，
    从而被标记为待复核。
    """
    payload = {
        "reagent_id": step.reagent_id,
        "reagent_name": step.reagent_name,
        "categories": step_categories(db, step),
        "time_offset_min": step.time_offset_min,
        "amount_ml": step.amount_ml,
        "note": step.note,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def confirmation_map(
    db: Session, plan_id: int
) -> dict[int, models.StepSafetyConfirmation]:
    rows = (
        db.query(models.StepSafetyConfirmation)
        .filter(models.StepSafetyConfirmation.plan_id == plan_id)
        .all()
    )
    return {r.order_index: r for r in rows}


def effective_status(
    row: models.StepSafetyConfirmation | None, fingerprint: str | None = None
) -> str:
    """确认状态：无记录=未确认；指纹对不上=待复核（防御性，正常路径已同步）。"""
    if row is None:
        return UNCONFIRMED
    if (
        row.status == CONFIRMED
        and fingerprint is not None
        and row.content_fingerprint != fingerprint
    ):
        return STALE
    return row.status


def reset_row(row: models.StepSafetyConfirmation | None) -> models.StepSafetyConfirmation:
    """清除确认内容并标记待复核（保留行与历史时间戳用于展示）。"""
    if row is not None:
        row.status = STALE
        row.measures = "[]"
        row.safety_note = None
        row.content_fingerprint = None
        row.confirmed_at = None
    return row


def sync_on_step_replace(
    db: Session,
    plan_id: int,
    new_steps: list[models.Step],
    previous_rows: dict[int, models.StepSafetyConfirmation] | None = None,
) -> None:
    """步骤整体替换后的确认同步。

    - 同 order_index 且内容指纹一致：保留（视为步骤未受影响）；
    - 同序号但指纹变化：清除确认、标记待复核；
    - 新出现的序号：插入未确认行；
    - 已删除的序号：删除其确认行。
    """
    if previous_rows is None:
        previous_rows = confirmation_map(db, plan_id)

    keep_ids: set[int] = set()
    for st in new_steps:
        fp = step_fingerprint(db, st)
        row = previous_rows.get(st.order_index)
        if row is None:
            row = models.StepSafetyConfirmation(
                plan_id=plan_id,
                order_index=st.order_index,
                status=UNCONFIRMED,
                measures="[]",
                content_fingerprint=fp,
            )
            db.add(row)
        elif row.status == CONFIRMED and row.content_fingerprint == fp:
            keep_ids.add(row.id)
            continue
        else:
            reset_row(row)
            row.content_fingerprint = fp
        keep_ids.add(row.id)

    for idx, row in previous_rows.items():
        if idx not in {s.order_index for s in new_steps}:
            db.delete(row)


def invalidate_for_reagent(db: Session, reagent_id: int) -> int:
    """试剂变更：引用该试剂的全部已确认步骤标记为待复核。返回受影响行数。"""
    step_plan_orders = (
        db.query(models.Step.plan_id, models.Step.order_index)
        .filter(models.Step.reagent_id == reagent_id)
        .all()
    )
    affected = 0
    for plan_id, order_index in step_plan_orders:
        row = (
            db.query(models.StepSafetyConfirmation)
            .filter_by(plan_id=plan_id, order_index=order_index)
            .first()
        )
        if row is not None and row.status == CONFIRMED:
            reset_row(row)
            affected += 1
    return affected


def _row_out(row: models.StepSafetyConfirmation | None, fingerprint: str | None) -> dict:
    status = effective_status(row, fingerprint)
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "measures": json.loads(row.measures or "[]") if row else [],
        "safety_note": row.safety_note if row else None,
        "confirmed_at": _iso(row.confirmed_at) if row else None,
        "updated_at": _iso(row.updated_at) if row else None,
    }


def step_safety_out(
    db: Session,
    step: models.Step,
    rows: dict[int, models.StepSafetyConfirmation] | None = None,
) -> dict:
    """单步安全确认状态（供方案详情/步骤序列化）。"""
    if rows is None:
        rows = confirmation_map(db, step.plan_id)
    fp = step_fingerprint(db, step)
    return _row_out(rows.get(step.order_index), fp) | {"order_index": step.order_index}


def safety_summary(
    db: Session, plan: models.Plan
) -> tuple[dict, dict[int, models.StepSafetyConfirmation]]:
    """汇总方案全部步骤的安全确认状态。返回 (摘要, 确认行映射)。"""
    ordered = sorted(plan.steps, key=lambda s: (s.order_index, s.id))
    rows = confirmation_map(db, plan.id)
    steps_out: list[dict] = []
    counts = {CONFIRMED: 0, STALE: 0, UNCONFIRMED: 0}
    for st in ordered:
        fp = step_fingerprint(db, st)
        row = rows.get(st.order_index)
        status = effective_status(row, fp)
        counts[status] += 1
        steps_out.append(
            {
                "order_index": st.order_index,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "measures": json.loads(row.measures or "[]") if row else [],
                "safety_note": row.safety_note if row else None,
                "confirmed_at": _iso(row.confirmed_at) if row else None,
                "updated_at": _iso(row.updated_at) if row else None,
            }
        )
    total = len(ordered)
    all_confirmed = total > 0 and counts[CONFIRMED] == total
    summary = {
        "total_steps": total,
        "confirmed": counts[CONFIRMED],
        "stale": counts[STALE],
        "unconfirmed": counts[UNCONFIRMED],
        "pending_count": total - counts[CONFIRMED],
        "all_confirmed": all_confirmed,
        "measures_catalog": SAFETY_MEASURES,
        "steps": steps_out,
    }
    return summary, rows


def report_safety_section(db: Session, plan: models.Plan, *, gate_applied: bool) -> dict:
    """推演报告中的安全确认快照（随报告/导出 JSON 自包含落盘）。"""
    summary, _ = safety_summary(db, plan)
    return {
        "gate": "required_before_formal_run" if gate_applied else "not_enforced",
        "gate_label": "正式推演前须全部确认" if gate_applied else "预检不强制安全确认",
        "all_confirmed": summary["all_confirmed"],
        "total_steps": summary["total_steps"],
        "confirmed": summary["confirmed"],
        "stale": summary["stale"],
        "unconfirmed": summary["unconfirmed"],
        "steps": summary["steps"],
    }


def attach_to_timeline(db: Session, plan: models.Plan, report: dict) -> None:
    """把每步的安全确认状态注入时间轴条目（按步骤序号对齐）。"""
    summary, _ = safety_summary(db, plan)
    by_order = {s["order_index"]: s for s in summary["steps"]}
    for item in report.get("timeline", []):
        info = by_order.get(item["order_index"])
        if info is not None:
            item["safety"] = {
                "status": info["status"],
                "status_label": info["status_label"],
                "measures": info["measures"],
                "safety_note": info["safety_note"],
                "confirmed_at": info["confirmed_at"],
            }
