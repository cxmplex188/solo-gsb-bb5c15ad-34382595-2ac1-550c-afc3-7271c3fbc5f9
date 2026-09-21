"""方案保存、步骤推演与 JSON 导出。"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import models, versioning
from ..database import get_db
from ..deduce import _step_categories, build_report, persist_run
from ..schemas import PlanCreate, PlanOut, PlanUpdate, StepOut

router = APIRouter(prefix="/api/plans", tags=["plans"])


def _plan_out(db: Session, plan: models.Plan) -> PlanOut:
    steps_out = []
    for s in sorted(plan.steps, key=lambda x: (x.order_index, x.id)):
        steps_out.append(
            StepOut(
                id=s.id,
                order_index=s.order_index,
                time_offset_min=s.time_offset_min,
                reagent_id=s.reagent_id,
                reagent_name=s.reagent_name,
                amount_ml=s.amount_ml,
                note=s.note,
                categories=_step_categories(db, s),
            )
        )
    effective = versioning.get_published_version(db, plan.id)
    return PlanOut(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        steps=steps_out,
        effective_rule_version=versioning.effective_summary(effective),
    )


def _build_step(db: Session, plan_id: int, st, index: int) -> models.Step:
    reagent_name = st.reagent_name or ""
    snapshot_categories: list[str] = st.categories or []
    if st.reagent_id is not None:
        reagent = db.get(models.Reagent, st.reagent_id)
        if reagent is None:
            raise HTTPException(404, f"试剂 id={st.reagent_id} 不存在")
        reagent_name = reagent.name
        snapshot_categories = []
    elif not reagent_name:
        raise HTTPException(422, f"第 {index + 1} 步必须选择试剂或填写临时试剂名")
    return models.Step(
        plan_id=plan_id,
        order_index=st.order_index,
        time_offset_min=st.time_offset_min,
        reagent_id=st.reagent_id,
        reagent_name=reagent_name,
        categories=json.dumps(snapshot_categories, ensure_ascii=False),
        amount_ml=st.amount_ml,
        note=st.note,
    )


def _replace_steps(db: Session, plan: models.Plan, steps_in) -> None:
    db.query(models.Step).filter(models.Step.plan_id == plan.id).delete()
    db.flush()
    for idx, st in enumerate(steps_in):
        db.add(_build_step(db, plan.id, st, idx))


@router.get("", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)):
    plans = db.query(models.Plan).order_by(models.Plan.updated_at.desc()).all()
    return [_plan_out(db, p) for p in plans]


@router.post("", response_model=PlanOut, status_code=201)
def create_plan(plan_in: PlanCreate, db: Session = Depends(get_db)):
    plan = models.Plan(name=plan_in.name, description=plan_in.description)
    db.add(plan)
    db.flush()
    _replace_steps(db, plan, plan_in.steps)
    db.commit()
    db.refresh(plan)
    return _plan_out(db, plan)


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    return _plan_out(db, plan)


@router.put("/{plan_id}", response_model=PlanOut)
def update_plan(plan_id: int, plan_in: PlanUpdate, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    if plan_in.name is not None:
        plan.name = plan_in.name
    if plan_in.description is not None:
        plan.description = plan_in.description
    if plan_in.steps is not None:
        _replace_steps(db, plan, plan_in.steps)
    db.commit()
    db.refresh(plan)
    return _plan_out(db, plan)


@router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    db.delete(plan)
    db.commit()


@router.post("/{plan_id}/simulate")
def simulate_plan(
    plan_id: int,
    persist: bool = True,
    rule_version_id: int | None = Query(
        default=None,
        description="预览指定规则版本（含草稿）；仅 persist=false 时允许。"
                    "正式判定（persist=true）固定读取当前生效版本。",
    ),
    db: Session = Depends(get_db),
):
    """按规则版本推演方案。

    - persist=true（默认）：只读取方案当前生效规则版本；没有则回落全局规则图，
      并保存一条推演记录；
    - persist=false 且指定 rule_version_id：可用任意版本（含草稿）做发布前预览，
      不落盘、不改变判定口径。
    """
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    if not plan.steps:
        raise HTTPException(422, "方案还没有加液步骤，无法推演")
    if persist and rule_version_id is not None:
        raise HTTPException(422, "保存推演记录时不能指定规则版本：正式判定固定使用当前生效版本")

    report, resolved_vid = build_report(db, plan, rule_version_id=rule_version_id)
    report["plan_id"] = plan.id
    run_id = persist_run(db, plan, report, persist=persist, rule_version_id=resolved_vid)
    report["run_id"] = run_id
    return report


@router.post("/simulate-dry")
def simulate_dry(plan_in: PlanCreate, db: Session = Depends(get_db)):
    """不保存方案，直接对提交内容做一次推演（用于保存前预检）。

    预检对象尚未保存、没有方案级规则版本，使用全局限定规则图。
    """
    if not plan_in.steps:
        raise HTTPException(422, "方案还没有加液步骤，无法推演")
    plan = models.Plan(name=plan_in.name, description=plan_in.description)
    for idx, st in enumerate(plan_in.steps):
        plan.steps.append(_build_step(db, plan_id=0, st=st, index=idx))
    report, _ = build_report(db, plan)
    report["plan_id"] = None
    return report


def _export_headers(filename: str):
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }


@router.get("/{plan_id}/export")
def export_plan_report(plan_id: int, db: Session = Depends(get_db)):
    """用当前生效规则版本（无则全局规则图）实时推演并导出 JSON 文件。"""
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    report, _ = build_report(db, plan)
    report["plan_id"] = plan.id
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    return JSONResponse(
        json.loads(payload),
        headers=_export_headers(f"plan-{plan_id}-report.json"),
    )
