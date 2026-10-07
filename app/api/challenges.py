"""社区航线挑战 API：版本化发布 / 飞行记录幂等提交 / 审核 / 申诉与复核 / 排行榜与回放 / 解锁联动。"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from app.core.database import SessionLocal
from app.schemas import Action, validate_actions
from app.services import physics
from app.services import challenges as ch_svc

router = APIRouter(prefix="/api/challenges")


class MilestoneIn(BaseModel):
    id: Optional[str] = None
    kind: str
    name: str = ""
    planet_id: Optional[str] = None
    dist: Optional[float] = None
    r: Optional[float] = None


class UnlockRuleIn(BaseModel):
    type: str = "none"          # none / builtin_level / challenge / stars_total
    level_id: Optional[int] = None
    challenge_id: Optional[int] = None
    value: Optional[int] = None


class ReviewStageIn(BaseModel):
    """一个审核级：级别名 + 角色 + 指定审核人白名单 + 时限（小时，null=不限）。"""
    name: str = ""
    role: str = "reviewer"     # reviewer / moderator
    reviewers: List[str] = Field(default_factory=list)
    time_limit_hours: Optional[float] = None


class DefinitionIn(BaseModel):
    """一版关卡定义：燃料预算 + 时间限制 + 里程碑 + 多级审核链。"""
    name: str = ""
    brief: str = ""
    hint: str = ""
    budget_dv: float
    t_max: float
    milestones: List[MilestoneIn] = Field(default_factory=list)
    # 缺省（未提供）= 新建按默认单级 / 发新版沿用上版；显式 null 或 [] = 回到默认单级
    review_pipeline: Optional[List[ReviewStageIn]] = None


class CreateChallengeIn(DefinitionIn):
    title: str
    author: str = ""
    unlock_rule: Optional[UnlockRuleIn] = None


class VersionIn(DefinitionIn):
    unlock_rule: Optional[UnlockRuleIn] = None  # 缺省保留原条件；{"type":"none"} 清除


class SimIn(BaseModel):
    actions: List[Action] = Field(default_factory=list)
    version: Optional[int] = None  # 缺省游玩当前版本


class SubmitIn(BaseModel):
    """成绩提交：run_id 关联执行档案（服务端结算），submission_id 为幂等键。"""
    run_id: str
    submission_id: Optional[str] = None
    player: Optional[str] = None


class ReviewIn(BaseModel):
    action: str                 # approve / reject
    note: Optional[str] = ""


class AppealIn(BaseModel):
    """玩家申诉：署名须与提交一致；appeal_id 为幂等键。"""
    player: str
    reason: str
    appeal_id: Optional[str] = None


class AppealDecisionIn(BaseModel):
    """复核员裁决：uphold=维持原判 / overturn=推翻改判通过。"""
    decision: str
    note: Optional[str] = ""


class ModerationIn(BaseModel):
    """复核员撤销(revoke)/恢复(restore)上榜成绩。"""
    note: Optional[str] = ""


class ReviewerIn(BaseModel):
    """复核员注册新审核账号。"""
    name: str
    token: str
    role: str = "reviewer"      # reviewer / moderator


def _dump(model) -> dict:
    """pydantic v1/v2 兼容的 dict 导出（剔除未提供的字段）。"""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _definition(req: DefinitionIn) -> dict:
    d = _dump(req)
    d["milestones"] = [_dump(m) for m in req.milestones]
    d.pop("review_pipeline", None)
    return d


def _pipeline_arg(req: DefinitionIn):
    """审核链入参归一：字段未出现 → UNSET（沿默认/沿用上版）；出现（含 null）→ 列表/None。"""
    fields_set = getattr(req, "model_fields_set", None) or getattr(req, "__fields_set__", set())
    if "review_pipeline" not in fields_set:
        return ch_svc._PIPELINE_UNSET
    if req.review_pipeline is None:
        return None
    return [_dump(s) for s in req.review_pipeline]


def _sim_dict(r: dict, challenge_id: int, version: int) -> dict:
    return {
        "ok": r["ok"],
        "reason": r["reason"],
        "elapsed_days": r["elapsed_days"],
        "fuel_used": r["fuel_used"],
        "budget_dv": r["budget_dv"],
        "milestones": r["milestones"],
        "events": r["events"],
        "trajectory": r["trajectory"],
        "challenge_id": challenge_id,
        "version": version,
    }


def _token(v) -> Optional[str]:
    """直接函数调用（测试）时 Header 参数未解析为 FieldInfo，归一化为 None。"""
    return v if isinstance(v, str) else None


def _error(e: Exception) -> HTTPException:
    if isinstance(e, (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound,
                      ch_svc.RunNotFound, ch_svc.SubmissionNotFound,
                      ch_svc.AppealNotFound)):
        return HTTPException(404, str(e))
    if isinstance(e, ch_svc.UnauthorizedReviewer):
        return HTTPException(401, str(e))
    if isinstance(e, ch_svc.ForbiddenReviewer):
        return HTTPException(403, str(e))
    if isinstance(e, ch_svc.StageForbidden):
        return HTTPException(403, str(e))
    if isinstance(e, ch_svc.ChallengeLocked):
        return HTTPException(403, str(e))
    if isinstance(e, (ch_svc.ReviewConflict, ch_svc.AppealConflict)):
        return HTTPException(409, str(e))
    if isinstance(e, ch_svc.ReviewerConflict):
        return HTTPException(409, str(e))
    return HTTPException(400, str(e))


# ---------- 发布（版本化） ----------

@router.get("")
def list_challenges():
    """挑战列表：当前版本摘要 + 解锁状态 + 本机最佳（已审核）+ 待审数。"""
    with SessionLocal() as db:
        return {"challenges": ch_svc.list_challenges(db)}


@router.post("", status_code=201)
def create_challenge(req: CreateChallengeIn):
    """发布新挑战：校验定义（含多级审核链）后创建挑战主体与不可变的 v1 版本。"""
    try:
        with SessionLocal() as db:
            return ch_svc.create_challenge(
                db, title=req.title, author=req.author,
                definition=_definition(req),
                unlock_rule=_dump(req.unlock_rule) if req.unlock_rule else None,
                review_pipeline=_pipeline_arg(req))
    except (ch_svc.ValidationError,) as e:
        raise _error(e)


@router.get("/review_queue")
def review_queue(status: str = "pending", limit: int = 100,
                 level: Optional[int] = None, overdue: Optional[bool] = None):
    """全站成绩提交队列（默认待审核）。

    可按审核级 ?level=1..n 与该级 SLA 是否超时 ?overdue=true 过滤；
    每条返回当前级 stage（级别名/指定审核人/时限 due_at/overdue）。
    """
    with SessionLocal() as db:
        return {"submissions": ch_svc.list_submissions(
            db, status=status, limit=limit, level=level, overdue=overdue)}


@router.get("/submissions/{record_id}")
def get_submission(record_id: int):
    """成绩详情：审核通过后附带动作方案与轨迹（供回放），并附申诉状态。"""
    with SessionLocal() as db:
        detail = ch_svc.submission_detail(db, record_id)
    if detail is None:
        raise HTTPException(404, "成绩记录不存在")
    return detail


@router.get("/submissions/{record_id}/timeline")
def get_timeline(record_id: int):
    """成绩全链路时间线：提交→初审→申诉→复核→撤销/恢复（可追溯事件档案）。"""
    with SessionLocal() as db:
        tl = ch_svc.submission_timeline(db, record_id)
    if tl is None:
        raise HTTPException(404, "成绩记录不存在")
    return tl


@router.post("/submissions/{record_id}/appeal", status_code=201)
def appeal_submission(record_id: int, req: AppealIn):
    """玩家对驳回/撤销的成绩发起申诉（appeal_id 幂等），成绩进入复核队列。"""
    try:
        with SessionLocal() as db:
            return ch_svc.create_appeal(
                db, record_id=record_id, player=req.player,
                reason=req.reason, appeal_id=req.appeal_id)
    except (ch_svc.SubmissionNotFound, ch_svc.ValidationError,
            ch_svc.ForbiddenReviewer, ch_svc.AppealConflict) as e:
        raise _error(e)


@router.get("/appeals")
def appeals(status: str = "pending", limit: int = 50,
            level: Optional[int] = None):
    """申诉队列。

    不带 ?level=：仅返回旧版默认链路、由复核员统一裁决的申诉；
    ?level=N：返回路由到第 N 级审核队列的多级链路申诉（由该级审核人处理）。
    """
    with SessionLocal() as db:
        return {"appeals": ch_svc.list_appeals(
            db, status=status, limit=limit, level=level)}


@router.get("/mine/submissions")
def my_submissions(player: str, challenge_id: Optional[int] = None,
                   version: Optional[int] = None, limit: int = 50):
    """玩家按署名查询本人提交：审核状态/申诉入口/被撤销与回滚情况。"""
    with SessionLocal() as db:
        return {"submissions": ch_svc.player_submissions(
            db, player, challenge_id=challenge_id, version=version,
            limit=limit)}


@router.post("/appeals/{appeal_id}/decision")
def decide_appeal(appeal_id: int, req: AppealDecisionIn,
                  x_reviewer_token: Optional[str] = Header(default=None)):
    """复核员裁决申诉：uphold 维持 / overturn 推翻改判通过（含回滚联动效果）。"""
    try:
        with SessionLocal() as db:
            return ch_svc.decide_appeal(
                db, appeal_id_key=appeal_id, decision=req.decision,
                note=req.note or "", moderator_token=_token(x_reviewer_token))
    except (ch_svc.AppealNotFound, ch_svc.ValidationError,
            ch_svc.UnauthorizedReviewer, ch_svc.ForbiddenReviewer,
            ch_svc.SubmissionNotFound, ch_svc.ReviewConflict) as e:
        raise _error(e)


@router.post("/submissions/{record_id}/revoke")
def revoke_submission(record_id: int, req: ModerationIn,
                      x_reviewer_token: Optional[str] = Header(default=None)):
    """复核员撤销上榜成绩：出榜 + 关闭回放 + 级联回收解锁（幂等）。"""
    try:
        with SessionLocal() as db:
            return ch_svc.revoke(db, record_id=record_id, note=req.note or "",
                                 moderator_token=_token(x_reviewer_token))
    except (ch_svc.SubmissionNotFound, ch_svc.UnauthorizedReviewer,
            ch_svc.ForbiddenReviewer, ch_svc.ReviewConflict) as e:
        raise _error(e)


@router.post("/submissions/{record_id}/restore")
def restore_submission(record_id: int, req: ModerationIn,
                       x_reviewer_token: Optional[str] = Header(default=None)):
    """复核员恢复被撤销的成绩：重新上榜/回放/解锁（幂等）。"""
    try:
        with SessionLocal() as db:
            return ch_svc.restore(db, record_id=record_id, note=req.note or "",
                                  moderator_token=_token(x_reviewer_token))
    except (ch_svc.SubmissionNotFound, ch_svc.UnauthorizedReviewer,
            ch_svc.ForbiddenReviewer, ch_svc.ReviewConflict) as e:
        raise _error(e)


@router.get("/reviewers/me")
def reviewer_me(x_reviewer_token: Optional[str] = Header(default=None)):
    """查询当前审核身份：无 token 时返回内置初审员（本地单机开放入口）。"""
    try:
        with SessionLocal() as db:
            rv = ch_svc.authenticate(db, _token(x_reviewer_token),
                                     required_role=ch_svc.ROLE_REVIEWER)
        return {"name": rv.name, "role": rv.role}
    except (ch_svc.UnauthorizedReviewer, ch_svc.ForbiddenReviewer) as e:
        raise _error(e)


@router.get("/reviewers")
def list_reviewers():
    """审核账号名单（id/署名/角色，不含令牌）：配置审核级指定审核人白名单用。"""
    with SessionLocal() as db:
        return {"reviewers": ch_svc.list_reviewers(db)}


@router.post("/reviewers", status_code=201)
def create_reviewer(req: ReviewerIn,
                    x_reviewer_token: Optional[str] = Header(default=None)):
    """复核员注册新的初审员/复核员账号。"""
    try:
        with SessionLocal() as db:
            actor = ch_svc.authenticate(db, _token(x_reviewer_token),
                                        required_role=ch_svc.ROLE_MODERATOR)
            return ch_svc.register_reviewer(
                db, token=req.token, name=req.name, role=req.role, actor=actor)
    except (ch_svc.UnauthorizedReviewer, ch_svc.ForbiddenReviewer,
            ch_svc.ValidationError, ch_svc.ReviewerConflict) as e:
        raise _error(e)


@router.post("/submissions/{record_id}/review")
def review_submission(record_id: int, req: ReviewIn,
                      x_reviewer_token: Optional[str] = Header(default=None),
                      level: Optional[int] = None):
    """在当前审核级裁决：逐级通过直至终审上榜，或在某级驳回（按版本审核链鉴权）。

    ?level=N 为队列重试防串级参数：成绩当前不在第 N 级时返回 409。
    申诉路由到某级的成绩也在本接口由该级指定审核人处理（维持/推翻）。
    """
    try:
        with SessionLocal() as db:
            return ch_svc.review(db, record_id=record_id,
                                 action=req.action, note=req.note or "",
                                 reviewer_token=_token(x_reviewer_token),
                                 expect_level=level)
    except (ch_svc.ValidationError, ch_svc.SubmissionNotFound,
            ch_svc.ReviewConflict, ch_svc.UnauthorizedReviewer,
            ch_svc.ForbiddenReviewer, ch_svc.StageForbidden,
            ch_svc.VersionNotFound) as e:
        raise _error(e)


@router.get("/{challenge_id}")
def challenge_detail(challenge_id: int):
    """挑战详情：版本列表 + 当前版本完整定义 + 解锁状态。"""
    try:
        with SessionLocal() as db:
            return ch_svc.challenge_detail(db, challenge_id)
    except ch_svc.ChallengeNotFound as e:
        raise _error(e)


@router.post("/{challenge_id}/versions", status_code=201)
def publish_version(challenge_id: int, req: VersionIn):
    """发布新版本：定义（含审核链）落库为不可变版本并设为当前版本（旧版本保留）。

    审核链缺省沿用上一版本；显式 null/[] 回到默认单级；显式列表则按新配置。
    """
    try:
        with SessionLocal() as db:
            kwargs = {}
            if req.unlock_rule is not None:
                kwargs["unlock_rule"] = _dump(req.unlock_rule)
            pipeline = _pipeline_arg(req)
            if pipeline is not ch_svc._PIPELINE_UNSET:
                kwargs["review_pipeline"] = pipeline
            return ch_svc.publish_version(db, challenge_id,
                                          definition=_definition(req), **kwargs)
    except (ch_svc.ValidationError, ch_svc.ChallengeNotFound) as e:
        raise _error(e)


@router.get("/{challenge_id}/versions/{version}")
def version_detail(challenge_id: int, version: int):
    """指定版本的完整定义（旧版本仍可查看）。"""
    try:
        with SessionLocal() as db:
            return ch_svc.version_detail(db, challenge_id, version)
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)


# ---------- 飞行记录：预览 / 执行 / 幂等提交 ----------

@router.post("/{challenge_id}/preview")
def preview(challenge_id: int, req: SimIn):
    """实时预览轨迹（不评分、不落库、不校验解锁）。"""
    try:
        with SessionLocal() as db:
            ch, ver = ch_svc.get_playable(db, challenge_id, req.version)
            lv = ch_svc.level_def(ver)
            cid, v_no = ch.id, ver.version
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)
    r = physics.integrate(lv, validate_actions(req.actions))
    return _sim_dict(r, cid, v_no)


@router.post("/{challenge_id}/run")
def run(challenge_id: int, req: SimIn):
    """执行挑战飞行：服务端结算（含星级），执行档案落库并返回 run_id。

    挑战未解锁时拒绝结算（403）。
    """
    try:
        with SessionLocal() as db:
            ch, ver = ch_svc.get_playable(db, challenge_id, req.version,
                                          enforce_unlock=True)
            lv = ch_svc.level_def(ver)
            actions = validate_actions(req.actions)
            r = physics.integrate(lv, actions)
            stars = physics.stars_for(lv, r["fuel_used"], r["ok"])
            rec = ch_svc.record_run(db, ch, ver, actions, r, stars)
            resp = _sim_dict(r, ch.id, ver.version)
            resp["stars"] = stars
            resp["run_id"] = rec.run_uid
            return resp
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound,
            ch_svc.ChallengeLocked) as e:
        raise _error(e)


@router.post("/{challenge_id}/submit")
def submit(challenge_id: int, req: SubmitIn):
    """提交成绩：幂等键去重，落库为待审核；审核通过后才上榜/回放/联动解锁。"""
    try:
        with SessionLocal() as db:
            return ch_svc.submit(db, challenge_id=challenge_id,
                                 run_id=req.run_id,
                                 submission_id=req.submission_id,
                                 player=req.player)
    except (ch_svc.RunNotFound, ch_svc.RunChallengeMismatch) as e:
        raise _error(e)


@router.get("/{challenge_id}/submissions")
def challenge_submissions(challenge_id: int, status: str = "pending",
                          limit: int = 50):
    """某挑战的成绩提交列表（默认待审核队列）。"""
    try:
        with SessionLocal() as db:
            ch_svc.get_playable(db, challenge_id)  # 存在性校验
            return {"submissions": ch_svc.list_submissions(
                db, challenge_id=challenge_id, status=status, limit=limit)}
    except ch_svc.ChallengeNotFound as e:
        raise _error(e)


@router.get("/{challenge_id}/leaderboard")
def get_leaderboard(challenge_id: int, version: Optional[int] = None,
                    limit: int = 50):
    """排行榜：仅审核通过的成绩，按版本结算，每名玩家取最佳一条。"""
    try:
        with SessionLocal() as db:
            return ch_svc.leaderboard(db, challenge_id, version=version,
                                      limit=limit)
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)
