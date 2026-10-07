"""社区航线挑战服务：版本化发布 / 幂等提交 / 多级审核 / 申诉与复核 / 排行榜 / 回放 / 解锁联动。

设计要点：
- 版本化：每次发布生成不可变的 ChallengeVersion（预算 + 里程碑 + 时间限制 + 审核链），
  飞行记录与成绩都锚定具体版本；排行榜按版本结算，旧版本仍可查看与回放。
- 多级审核（按版本配置）：设计者在发布版本时配置 1~MAX_REVIEW_STAGES 个审核级，
  每级指定级别名、角色（reviewer/moderator）、指定审核人署名白名单与时限（SLA）。
  提交进入第 1 级 pending 队列；逐级通过（review_advance）后进入下一级，
  全部级通过才终审 approved 并联动排行榜/回放/解锁；任一级驳回即 rejected。
  版本未配置审核链时沿用旧版默认单级（初审员一审，申诉统一进复核员队列）。
- 申诉进入正确队列：对被第 L 级驳回的成绩申诉，成绩回到第 L 级 pending 队列
  （ChallengeAppeal.target_level=L），由该级指定审核人在普通审核接口裁决——
  维持原判回到原状态、推翻原判则继续向 L+1 级流转（终审级推翻即上榜）；
  撤销（终审后）成绩的申诉回到终审级。旧版默认链路 target_level 为空，
  仍由复核员在复核裁决接口处理。
- 时限（SLA）：每级可配 time_limit_hours；队列返回应处理时间 due_at 与 overdue
  标记。超时不自动改判，moderator 可在任意级兜底处理（含超时队列）。
- 幂等结算：/run 落库执行档案（ChallengeRun），/submit 以 submission_id 为幂等键
  关联档案落库成绩；重复提交（双击/重试/多标签页）返回首个结果，不重复计数。
- 审核联动：只有终审 approved 的成绩才进入排行榜、开放轨迹回放，并计入解锁条件。
  解锁状态按规则实时求值；撤销(revoke)/恢复(restore)由复核员执行，
  撤销即自动出榜、关闭回放并级联回收解锁，接口返回前后 diff 的回滚效果。
- 可追溯：每次状态迁移追加一条不可变的 ChallengeReviewEvent（提交/逐级通过/
  终审通过/驳回/申诉/复核/撤销/恢复）；历史已审核成绩在启动时补登 legacy 事件。
- 审核权限：Reviewer 分 reviewer（初审）/moderator（复核、撤销、注册、超时兜底）；
  各级再以署名白名单限定到指定审核人；token 经 X-Reviewer-Token 上送；
  内置本地账号开箱即用，旧版默认单级缺省调用降级为内置初审员。
- 并发安全：模块级锁串行化提交/审核/申诉临界区（SQLite 单写者），唯一约束兜底。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from app.models import (Challenge, ChallengeAppeal, ChallengeReviewEvent,
                        ChallengeRun, ChallengeSubmission, ChallengeVersion,
                        LevelScore, Reviewer)
from app.services import physics
from app.services.levels import LEVEL_BY_ID

# SQLite 单写者：序列化"查重-插入 / 审核状态迁移 / 申诉复核"临界区。
_LOCK = threading.Lock()

_KEEP_RULE = object()  # publish_version 的"保留原解锁条件"哨兵

_PLANET_IDS = {b["id"] for b in physics.BODIES if b["id"] != "sun"}
_MILESTONE_KINDS = {"proximity", "radius", "assist_capture", "assist_then_radius", "escape"}
_REVIEW_ACTIONS = {"approve": "approved", "reject": "rejected"}
DEFAULT_PLAYER = "匿名飞行员"

# 审核状态
PENDING, APPROVED, REJECTED, REVOKED = "pending", "approved", "rejected", "revoked"
# 申诉裁决
APPEAL_PENDING, APPEAL_UPHOLD, APPEAL_OVERTURN = "pending", "uphold", "overturn"
# 事件类型
EV_SUBMIT = "submit"
EV_REVIEW = {"approved": "review_approve", "rejected": "review_reject"}
EV_ADVANCE = "review_advance"  # 多级链路：通过当前级，进入下一级（非终审）
EV_APPEAL = "appeal"
EV_APPEAL_DECISION = {APPEAL_UPHOLD: "appeal_uphold", APPEAL_OVERTURN: "appeal_overturn"}
EV_REVOKE = "revoke"
EV_RESTORE = "restore"
EV_LEGACY = "legacy"

ROLE_REVIEWER = "reviewer"
ROLE_MODERATOR = "moderator"
MAX_APPEAL_ROUNDS = 2
MAX_APPEAL_REASON = 500

# 多级审核链配置
MAX_REVIEW_STAGES = 5
MAX_STAGE_REVIEWERS = 10
MIN_STAGE_HOURS, MAX_STAGE_HOURS = 0.5, 720.0  # 每级时限（小时）

# 内置审核账号（本地单机开箱即用；正式部署可禁用/改 token）
BUILTIN_REVIEWERS = [
    {"name": "本机初审员", "token": "local-reviewer", "role": ROLE_REVIEWER},
    {"name": "本机复核员", "token": "local-moderator", "role": ROLE_MODERATOR},
]


# ---------- 异常 ----------

class ChallengeNotFound(KeyError):
    """挑战不存在。"""


class VersionNotFound(KeyError):
    """挑战版本不存在。"""


class SubmissionNotFound(KeyError):
    """成绩提交记录不存在。"""


class ValidationError(ValueError):
    """关卡定义 / 解锁条件 / 请求参数校验失败。"""


class RunNotFound(KeyError):
    """run_id 对应的挑战执行档案不存在。"""


class RunChallengeMismatch(ValueError):
    """执行档案与提交的挑战不一致。"""


class ChallengeLocked(PermissionError):
    """挑战未解锁，不能结算飞行记录。"""


class ReviewConflict(RuntimeError):
    """成绩已终审或申诉链路状态冲突，不能执行该动作。"""


class UnauthorizedReviewer(PermissionError):
    """缺少有效的审核凭证（401）。"""


class ForbiddenReviewer(PermissionError):
    """审核账号权限不足（403）。"""


class AppealNotFound(KeyError):
    """申诉单不存在。"""


class AppealConflict(RuntimeError):
    """已有进行中的申诉 / 申诉轮次用尽 / 当前状态不能申诉。"""


class ReviewerConflict(ValueError):
    """审核员署名或 token 冲突。"""


class StageForbidden(PermissionError):
    """审核人不属于该版本该审核级的指定名单（403）。"""


# ---------- 定义校验 ----------

def _clean_text(s, max_len: int, field: str, allow_empty: bool = False) -> str:
    s = (s or "").strip()
    if not s and not allow_empty:
        raise ValidationError(f"{field}不能为空")
    if len(s) > max_len:
        raise ValidationError(f"{field}过长（≤{max_len} 字）")
    return s


def _bounded(v, lo: float, hi: float, field: str) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if not lo <= v <= hi:
        raise ValidationError(f"{field}需在 {lo}~{hi} 之间")
    return v


def _planet(pid) -> str:
    if pid not in _PLANET_IDS:
        raise ValidationError(f"未知行星: {pid}")
    return pid


def validate_milestones(ms_list) -> List[dict]:
    """校验并规范化里程碑定义（与物理引擎支持的判定类型对齐）。"""
    if not isinstance(ms_list, list) or not 1 <= len(ms_list) <= 5:
        raise ValidationError("里程碑需 1~5 个")
    out, seen = [], set()
    for i, m in enumerate(ms_list, 1):
        kind = (m or {}).get("kind")
        if kind not in _MILESTONE_KINDS:
            raise ValidationError(f"未知里程碑类型: {kind}")
        mid = str(m.get("id") or f"m{i}")
        if mid in seen:
            raise ValidationError(f"里程碑 id 重复: {mid}")
        seen.add(mid)
        item = {"id": mid, "kind": kind,
                "name": _clean_text(m.get("name"), 40, "里程碑名称")}
        if kind == "proximity":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["dist"] = _bounded(m.get("dist"), 0.001, 5.0, "飞掠距离")
        elif kind == "radius":
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "assist_capture":
            item["planet_id"] = _planet(m.get("planet_id"))
        elif kind == "assist_then_radius":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "escape":
            item["r"] = _bounded(m.get("r"), 1.0, 100.0, "逃逸边界")
        out.append(item)
    return out


def validate_definition(d: dict) -> dict:
    """校验一版关卡定义：预算 / 时间限制 / 里程碑。"""
    d = d or {}
    return {
        "name": _clean_text(d.get("name"), 40, "版本名", allow_empty=True),
        "brief": _clean_text(d.get("brief"), 500, "简介", allow_empty=True),
        "hint": _clean_text(d.get("hint"), 500, "提示", allow_empty=True),
        "budget_dv": _bounded(d.get("budget_dv"), 0.0002, 0.05, "燃料预算"),
        "t_max": _bounded(d.get("t_max"), 30, 20000, "时间限制"),
        "milestones": validate_milestones(d.get("milestones") or []),
    }


def validate_unlock_rule(db, rule, self_id: Optional[int] = None) -> Optional[dict]:
    """校验解锁条件；{"type":"none"} 与 None 都表示无条件（返回 None）。"""
    if not rule or rule.get("type") in (None, "none"):
        return None
    t = rule.get("type")
    if t == "builtin_level":
        lid = rule.get("level_id")
        if lid not in LEVEL_BY_ID:
            raise ValidationError(f"未知内置关卡: {lid}")
        return {"type": t, "level_id": lid}
    if t == "challenge":
        cid = rule.get("challenge_id")
        if self_id is not None and cid == self_id:
            raise ValidationError("解锁条件不能引用挑战自身")
        if db.query(Challenge).filter(Challenge.id == cid).first() is None:
            raise ValidationError(f"前置挑战不存在: {cid}")
        return {"type": t, "challenge_id": cid}
    if t == "stars_total":
        return {"type": t, "value": int(_bounded(rule.get("value"), 1, 99, "所需星数"))}
    raise ValidationError(f"未知解锁条件类型: {t}")


# ---------- 多级审核链配置 ----------

def _legacy_pipeline() -> List[dict]:
    """旧版默认单级链路：初审员一审、无时限、申诉统一进复核员队列。"""
    return [{"level": 1, "name": "初审", "role": ROLE_REVIEWER,
             "reviewers": [], "time_limit_hours": None, "legacy": True}]


def validate_review_pipeline(db, pipeline, *, current_levels: Optional[List[str]] = None) -> List[dict]:
    """校验并规范化版本的多级审核链配置。

    入参：[{"name": 级别名, "role": reviewer/moderator,
            "reviewers": [审核员署名…], "time_limit_hours": 数字或 null}]
    返回存储形态：[{"level", "name", "role", "reviewers"(排序去重),
                 "time_limit_hours", "legacy"=False}]
    None/[] 表示沿用旧版默认单级（current_levels 仅用于错误提示）。
    指定审核人须是已注册且启用的账号；角色为 moderator 的级别只允许指定复核员，
    reviewer 级别不限制被指定人的角色（moderator 本就有权处理任意级）。
    """
    if pipeline is None:
        return _legacy_pipeline()
    if not isinstance(pipeline, list):
        raise ValidationError("审核链必须是审核级列表")
    if not 1 <= len(pipeline) <= MAX_REVIEW_STAGES:
        raise ValidationError(f"审核级数量需在 1~{MAX_REVIEW_STAGES} 之间")
    where = ("配置" if not current_levels
             else f"（当前共 {len(current_levels)} 级）")
    active = {r.name: r for r in db.query(Reviewer).filter(Reviewer.active == 1).all()}
    out, names = [], set()
    for i, st in enumerate(pipeline, 1):
        if not isinstance(st, dict):
            raise ValidationError(f"第 {i} 级审核配置格式有误{where}")
        name = _clean_text(st.get("name"), 24, f"第 {i} 级级别名",
                           allow_empty=True) or f"第 {i} 级"
        role = st.get("role") or ROLE_REVIEWER
        if role not in (ROLE_REVIEWER, ROLE_MODERATOR):
            raise ValidationError(f"第 {i} 级角色未知: {role}{where}")
        raw_reviewers = st.get("reviewers") or []
        if not isinstance(raw_reviewers, list):
            raise ValidationError(f"第 {i} 级指定审核人必须是列表{where}")
        if len(raw_reviewers) > MAX_STAGE_REVIEWERS:
            raise ValidationError(f"第 {i} 级指定审核人不超过 {MAX_STAGE_REVIEWERS} 人")
        reviewers = []
        for who in raw_reviewers:
            who = _clean_text(who, 24, f"第 {i} 级审核人署名", allow_empty=True)
            if not who or who in reviewers:
                continue
            rv = active.get(who)
            if rv is None:
                raise ValidationError(f"第 {i} 级指定审核人不存在或已停用: {who}")
            if role == ROLE_MODERATOR and rv.role != ROLE_MODERATOR:
                raise ValidationError(
                    f"第 {i} 级是复核级，指定审核人「{who}」不是复核员")
            reviewers.append(who)
        tlim = st.get("time_limit_hours")
        if tlim is not None and tlim != "":
            tlim = _bounded(tlim, MIN_STAGE_HOURS, MAX_STAGE_HOURS,
                            f"第 {i} 级时限（小时）")
        else:
            tlim = None
        if name in names:
            raise ValidationError(f"审核级别名重复: {name}")
        names.add(name)
        out.append({"level": i, "name": name, "role": role,
                    "reviewers": reviewers,
                    "time_limit_hours": (round(float(tlim), 2)
                                         if tlim is not None else None),
                    "legacy": False})
    return out


def pipeline_of(ver: Optional[ChallengeVersion]) -> List[dict]:
    """取版本的审核链；未配置（旧版本）返回旧版默认单级。"""
    raw = (ver.review_pipeline_json if ver is not None
           and ver.review_pipeline_json else "")
    if not raw:
        return _legacy_pipeline()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return _legacy_pipeline()
    return data if isinstance(data, list) and data else _legacy_pipeline()


def _stage_at(pipeline: List[dict], level: Optional[int]) -> dict:
    """审核级配置（1 起）；旧存档空级别归一到第 1 级；越界归终审级。"""
    idx = (level or 1) - 1
    if not 0 <= idx < len(pipeline):
        idx = len(pipeline) - 1
    return pipeline[idx]


def _current_level(rec: ChallengeSubmission) -> int:
    """成绩当前所处审核级：pending 记录取 review_level（旧存档归一到 1）。"""
    return max(1, rec.review_level or 1)


def _stage_view(stage: dict, entered_at: Optional[float], now: float) -> dict:
    """对外的审核级视图（含 SLA 应处理时间与超时标记）。"""
    limit = stage.get("time_limit_hours")
    due_at = (entered_at + limit * 3600.0
              if limit and entered_at else None)
    return {
        "level": stage["level"], "name": stage["name"], "role": stage["role"],
        "reviewers": list(stage.get("reviewers") or []),
        "time_limit_hours": limit,
        "due_at": due_at,
        "overdue": bool(due_at and now > due_at),
        "legacy": bool(stage.get("legacy")),
    }


def _stage_decision_view(db, rec: ChallengeSubmission, pipeline: List[dict]) -> dict:
    """一条待审成绩在当前级的视图：当前级 + 总级数 + 是否终审级 + SLA。"""
    level = _current_level(rec)
    stage = _stage_at(pipeline, level)
    view = _stage_view(stage, rec.review_stage_entered_at, time.time())
    view["total_levels"] = len(pipeline)
    view["final_level"] = level >= len(pipeline)
    return view


def _authorize_stage(db, pipeline: List[dict], level: int,
                     rv: Reviewer) -> dict:
    """校验审核人有权处理该版本的指定审核级，返回该级配置。

    - moderator 可处理任意级（含超时兜底）；
    - reviewer 仅能处理 role=reviewer 的级，且级别指定了署名白名单时必须在名单内；
    - 复核级（role=moderator）初审员无权处理。
    """
    stage = _stage_at(pipeline, level)
    if rv.role == ROLE_MODERATOR:
        return stage
    if stage["role"] == ROLE_MODERATOR:
        raise ForbiddenReviewer(
            f"第 {level} 级「{stage['name']}」是复核级，初审员无权处理")
    allowed = stage.get("reviewers") or []
    if allowed and rv.name not in allowed:
        raise StageForbidden(
            f"第 {level} 级「{stage['name']}」仅由指定审核人处理，"
            f"「{rv.name}」不在名单内")
    return stage


# ---------- 发布（版本化） ----------

def _add_version(db, challenge_id: int, version: int, defn: dict,
                 pipeline: Optional[List[dict]] = None) -> ChallengeVersion:
    # 旧版默认单级链路以空串落库（兼容旧版本/旧读取路径）；仅显式配置才存 JSON
    is_legacy = not pipeline or (len(pipeline) == 1 and pipeline[0].get("legacy"))
    ver = ChallengeVersion(
        challenge_id=challenge_id, version=version,
        name=defn["name"], brief=defn["brief"], hint=defn["hint"],
        budget_dv=defn["budget_dv"], t_max=defn["t_max"],
        milestones_json=json.dumps(defn["milestones"], ensure_ascii=False),
        review_pipeline_json="" if is_legacy else json.dumps(
            pipeline, ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(ver)
    return ver


_PIPELINE_UNSET = object()  # publish_version 的"审核链未提供"哨兵（区别于显式 None=默认单级）


def create_challenge(db, *, title: str, author: str, definition: dict,
                     unlock_rule=None,
                     review_pipeline=_PIPELINE_UNSET) -> dict:
    """发布新挑战：创建挑战主体 + 不可变的 v1 版本（含审核链配置）。"""
    title = _clean_text(title, 40, "挑战标题")
    author = _clean_text(author, 24, "设计者署名", allow_empty=True) or "匿名设计者"
    defn = validate_definition(definition)
    rule = validate_unlock_rule(db, unlock_rule)
    pipeline = (validate_review_pipeline(db, review_pipeline)
                if review_pipeline is not _PIPELINE_UNSET else _legacy_pipeline())
    ch = Challenge(title=title, author=author, status="published", current_version=1,
                   unlock_rule_json=json.dumps(rule) if rule else "",
                   created_at=time.time())
    db.add(ch)
    db.flush()  # 取 challenge.id
    _add_version(db, ch.id, 1, defn, pipeline)
    db.commit()
    return challenge_detail(db, ch.id)


def publish_version(db, challenge_id: int, definition: dict,
                    unlock_rule=_KEEP_RULE,
                    review_pipeline=_PIPELINE_UNSET) -> dict:
    """发布新版本：定义校验后落库为不可变版本并设为当前版本。

    unlock_rule 缺省（未传）时保留原解锁条件；显式传 {"type":"none"} 可清除。
    review_pipeline 缺省时沿用上一版本的审核链；显式传 None/[] 表示回到
    旧版默认单级；显式传审核级列表则按新配置（成绩按提交时版本的链路审核）。
    """
    ch = _get_challenge(db, challenge_id)
    defn = validate_definition(definition)
    prev = _get_version(db, challenge_id, ch.current_version)
    if review_pipeline is _PIPELINE_UNSET:
        pipeline = pipeline_of(prev)
    else:
        pipeline = validate_review_pipeline(
            db, review_pipeline,
            current_levels=[s.get("name", "") for s in pipeline_of(prev)])
    _add_version(db, ch.id, ch.current_version + 1, defn, pipeline)
    ch.current_version += 1
    if unlock_rule is not _KEEP_RULE:
        rule = validate_unlock_rule(db, unlock_rule, self_id=ch.id)
        ch.unlock_rule_json = json.dumps(rule) if rule else ""
    db.commit()
    return challenge_detail(db, ch.id)


# ---------- 查询与解锁 ----------

def _get_challenge(db, challenge_id: int) -> Challenge:
    ch = db.query(Challenge).filter(Challenge.id == challenge_id).first()
    if ch is None or ch.status == "archived":
        raise ChallengeNotFound(f"挑战不存在: {challenge_id}")
    return ch


def _get_version(db, challenge_id: int, version: int) -> ChallengeVersion:
    ver = (db.query(ChallengeVersion)
             .filter(ChallengeVersion.challenge_id == challenge_id,
                     ChallengeVersion.version == version)
             .first())
    if ver is None:
        raise VersionNotFound(f"挑战 #{challenge_id} 没有版本 v{version}")
    return ver


def level_def(ver: ChallengeVersion) -> dict:
    """把版本行转成物理引擎可用的关卡定义。"""
    return {
        "id": ver.challenge_id,
        "name": ver.name,
        "milestones": json.loads(ver.milestones_json),
        "budget_dv": ver.budget_dv,
        "t_max": ver.t_max,
    }


def _builtin_stars(db) -> dict:
    return {s.level_id: s.stars for s in db.query(LevelScore).all()}


def _challenge_stars(db) -> dict:
    """每个挑战已审核通过的最佳星数（跨版本）。"""
    out = {}
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == "approved")
              .all())
    for r in rows:
        out[r.challenge_id] = max(out.get(r.challenge_id, 0), r.stars)
    return out


def _unlock_state(rule: Optional[dict], builtin: dict, ch_stars: dict,
                  total_stars: int, titles: dict) -> Tuple[bool, Optional[str]]:
    """求值解锁条件，返回 (是否解锁, 未解锁时的说明)。"""
    if not rule:
        return True, None
    t = rule["type"]
    if t == "builtin_level":
        ok = builtin.get(rule["level_id"], 0) >= 1
        return ok, None if ok else f"通关内置第 {rule['level_id']} 关后解锁"
    if t == "challenge":
        ok = ch_stars.get(rule["challenge_id"], 0) >= 1
        dep = titles.get(rule["challenge_id"], f"#{rule['challenge_id']}")
        return ok, None if ok else f"通关挑战「{dep}」后解锁"
    if t == "stars_total":
        ok = total_stars >= rule["value"]
        return ok, None if ok else f"累计星数 ≥ {rule['value']} 后解锁"
    return True, None


def _unlock_context(db):
    builtin = _builtin_stars(db)
    ch_stars = _challenge_stars(db)
    total = sum(builtin.values()) + sum(ch_stars.values())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    return builtin, ch_stars, total, titles


def _rule_of(ch: Challenge) -> Optional[dict]:
    return json.loads(ch.unlock_rule_json) if ch.unlock_rule_json else None


def _best_from_rows(rows) -> dict:
    """按审核展示顺序（星数 → 燃料 → 用时 → 先到）取每组首条成绩。"""
    best = {}
    for r in rows:
        if r.challenge_id not in best:
            best[r.challenge_id] = {
                "stars": r.stars, "fuel_used": r.fuel_used,
                "elapsed_days": r.elapsed_days, "record_id": r.id,
                "version": r.version,
            }
    return best


def _approved_rows(db, *, current_versions: Optional[dict] = None):
    """已审核通过的成绩；可只取每个挑战的当前版本。"""
    q = db.query(ChallengeSubmission).filter(
        ChallengeSubmission.review_status == APPROVED)
    rows = q.order_by(ChallengeSubmission.stars.desc(),
                      ChallengeSubmission.fuel_used.asc(),
                      ChallengeSubmission.elapsed_days.asc(),
                      ChallengeSubmission.id.asc()).all()
    if current_versions is None:
        return rows
    return [r for r in rows
            if r.version == current_versions.get(r.challenge_id)]


def _best_approved(db) -> dict:
    """每个挑战跨版本的本机最佳已审核成绩（用于解锁；旧版本仍有效）。"""
    return _best_from_rows(_approved_rows(db))


def _current_versions(db) -> dict:
    return {c.id: c.current_version
            for c in db.query(Challenge).all()}


def _best_current_approved(db) -> dict:
    """每个挑战当前版本的本机最佳已审核成绩（用于挑战卡片/当前榜摘要）。"""
    return _best_from_rows(_approved_rows(
        db, current_versions=_current_versions(db)))


def _best_record_info(r: ChallengeSubmission) -> dict:
    return {
        "stars": r.stars, "fuel_used": r.fuel_used,
        "elapsed_days": r.elapsed_days, "record_id": r.id,
        "version": r.version, "player": r.player,
    }


def _leaderboard_maps(db) -> Tuple[dict, dict]:
    """返回 (每版本全局榜首, 每版本每名玩家的上榜成绩)。

    排行榜展示每名玩家在该版本的最佳一条；撤销/恢复时据此判断记录是否仍在榜。
    """
    version_best, player_best = {}, {}
    for r in _approved_rows(db):
        versions = version_best.setdefault(r.challenge_id, {})
        if r.version not in versions:
            versions[r.version] = _best_record_info(r)
        by_player = player_best.setdefault(r.challenge_id, {}).setdefault(r.version, {})
        if r.player not in by_player:
            by_player[r.player] = _best_record_info(r)
    return version_best, player_best


def list_challenges(db) -> List[dict]:
    """挑战列表：当前版本摘要 + 解锁状态 + 本机最佳（已审核）+ 待审数。"""
    rows = (db.query(Challenge)
              .filter(Challenge.status != "archived")
              .order_by(Challenge.id)
              .all())
    vers = {}
    for v in db.query(ChallengeVersion).all():
        vers.setdefault(v.challenge_id, {})[v.version] = v
    pend = {}
    for r in (db.query(ChallengeSubmission)
                .filter(ChallengeSubmission.review_status == "pending").all()):
        pend[r.challenge_id] = pend.get(r.challenge_id, 0) + 1
    builtin, ch_stars, total, titles = _unlock_context(db)
    best = _best_approved(db)
    best_current = _best_current_approved(db)
    out = []
    for ch in rows:
        v = vers.get(ch.id, {}).get(ch.current_version)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        out.append({
            "id": ch.id, "title": ch.title, "author": ch.author,
            "current_version": ch.current_version,
            "version_count": len(vers.get(ch.id, {})),
            "created_at": ch.created_at,
            "brief": v.brief if v else "",
            "budget_dv": v.budget_dv if v else 0.0,
            "t_max": v.t_max if v else 0.0,
            "milestone_count": len(json.loads(v.milestones_json)) if v else 0,
            "review_pipeline": _pipeline_summary(pipeline_of(v)) if v
            else _pipeline_summary(_legacy_pipeline()),
            "unlocked": unlocked, "unlock_desc": desc,
            "best": best.get(ch.id),
            "best_current": best_current.get(ch.id),
            "pending_count": pend.get(ch.id, 0),
        })
    return out


def challenge_detail(db, challenge_id: int) -> dict:
    """挑战详情：全部版本列表 + 当前版本完整定义 + 解锁状态。"""
    ch = _get_challenge(db, challenge_id)
    vers = (db.query(ChallengeVersion)
              .filter(ChallengeVersion.challenge_id == ch.id)
              .order_by(ChallengeVersion.version)
              .all())
    cur = next((v for v in vers if v.version == ch.current_version), vers[-1])
    builtin, ch_stars, total, titles = _unlock_context(db)
    rule = _rule_of(ch)
    unlocked, desc = _unlock_state(rule, builtin, ch_stars, total, titles)
    return {
        "id": ch.id, "title": ch.title, "author": ch.author,
        "status": ch.status, "current_version": ch.current_version,
        "created_at": ch.created_at,
        "unlock_rule": rule, "unlocked": unlocked, "unlock_desc": desc,
        "versions": [{
            "version": v.version, "name": v.name,
            "budget_dv": v.budget_dv, "t_max": v.t_max,
            "milestone_count": len(json.loads(v.milestones_json)),
            "review_pipeline": _pipeline_summary(pipeline_of(v)),
            "created_at": v.created_at,
        } for v in vers],
        "current": {
            "version": cur.version, "name": cur.name, "brief": cur.brief,
            "hint": cur.hint, "budget_dv": cur.budget_dv, "t_max": cur.t_max,
            "milestones": json.loads(cur.milestones_json),
            "review_pipeline": _pipeline_summary(pipeline_of(cur)),
        },
        "best": _best_approved(db).get(ch.id),
        "best_current": _best_current_approved(db).get(ch.id),
    }


def _pipeline_summary(pipeline: List[dict]) -> dict:
    """审核链摘要：级数 + 各级名/角色/时限（列表卡片与版本列表用）。"""
    return {"levels": len(pipeline),
            "legacy": bool(pipeline and pipeline[0].get("legacy")),
            "stages": [{"level": s["level"], "name": s["name"],
                        "role": s["role"],
                        "reviewers": list(s.get("reviewers") or []),
                        "time_limit_hours": s.get("time_limit_hours")}
                       for s in pipeline]}


def version_detail(db, challenge_id: int, version: int) -> dict:
    """指定版本的完整定义（含审核链；查看旧版本 / 按旧版本游玩）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version)
    return {
        "challenge_id": ch.id, "title": ch.title, "author": ch.author,
        "version": ver.version, "name": ver.name, "brief": ver.brief,
        "hint": ver.hint, "budget_dv": ver.budget_dv, "t_max": ver.t_max,
        "milestones": json.loads(ver.milestones_json),
        "review_pipeline": _pipeline_summary(pipeline_of(ver)),
        "is_current": ver.version == ch.current_version,
        "created_at": ver.created_at,
    }


def get_playable(db, challenge_id: int, version: Optional[int] = None,
                 enforce_unlock: bool = False) -> Tuple[Challenge, ChallengeVersion]:
    """取挑战 + 版本行；enforce_unlock 时未解锁抛 ChallengeLocked（结算类接口用）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version or ch.current_version)
    if enforce_unlock:
        builtin, ch_stars, total, titles = _unlock_context(db)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        if not unlocked:
            raise ChallengeLocked(desc or "挑战未解锁")
    return ch, ver


# ---------- 飞行记录：执行档案 + 幂等提交 ----------

def record_run(db, ch: Challenge, ver: ChallengeVersion, actions: List[dict],
               result: dict, stars: int) -> ChallengeRun:
    """把一次挑战飞行的完整结算落库，返回执行档案。"""
    run = ChallengeRun(
        run_uid=uuid.uuid4().hex,
        challenge_id=ch.id, version=ver.version,
        ok=1 if result["ok"] else 0,
        reason=result["reason"], stars=stars,
        fuel_used=result["fuel_used"], elapsed_days=result["elapsed_days"],
        actions_json=json.dumps(actions, ensure_ascii=False),
        trajectory_json=json.dumps(result["trajectory"]),
        events_json=json.dumps(result["events"], ensure_ascii=False),
        milestones_json=json.dumps(result["milestones"], ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _submitted_response(rec: ChallengeSubmission, duplicated: bool) -> dict:
    return {
        "saved": True,
        "duplicated": duplicated,
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "review_level": _current_level(rec) if rec.review_status == PENDING
        else rec.review_level,
        "stars": rec.stars,
    }


def submit(db, *, challenge_id: int, run_id: str, submission_id: Optional[str],
           player: Optional[str]) -> dict:
    """落库一条挑战成绩（默认待审核）。

    幂等：同一挑战下 submission_id 已存在时直接返回首个提交的结果
    （duplicated=True），不重复落库、不重复进入审核队列。幂等键的作用域
    仅限本挑战——不同挑战复用同一键是彼此独立的提交，不会串成绩；
    审核状态、排行榜与轨迹回放始终锚定提交路径上的这个挑战。
    """
    with _LOCK:
        # 1) 幂等重放：本挑战下同一提交已受理过 → 返回首个结果
        if submission_id:
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == submission_id,
                             ChallengeSubmission.challenge_id == challenge_id)
                     .first())
            if dup is not None:
                return _submitted_response(dup, duplicated=True)

        # 2) 关联执行档案：成绩以服务端结算为准，且必须属于本挑战
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.run_uid == run_id)
                 .first())
        if run is None:
            raise RunNotFound(f"执行记录不存在: {run_id}")
        if run.challenge_id != challenge_id:
            raise RunChallengeMismatch("执行记录与挑战不匹配")

        # 3) 落库成绩记录（(challenge_id, submission_id) 唯一约束兜底并发重复）
        now = time.time()
        rec = ChallengeSubmission(
            submission_id=submission_id or uuid.uuid4().hex,
            run_id=run.id,
            challenge_id=run.challenge_id,
            version=run.version,
            player=(player or "").strip()[:24] or DEFAULT_PLAYER,
            stars=run.stars,
            fuel_used=run.fuel_used,
            elapsed_days=run.elapsed_days,
            review_status="pending",
            review_level=1,                 # 从第 1 级审核队列开始逐级流转
            review_stage_entered_at=now,
            created_at=now,
        )
        db.add(rec)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == rec.submission_id,
                             ChallengeSubmission.challenge_id == challenge_id)
                     .first())
            if dup is not None:
                return _submitted_response(dup, duplicated=True)
            raise
        _add_event(db, rec, kind=EV_SUBMIT, actor=rec.player,
                   actor_role="player",
                   detail={"submission_id": rec.submission_id,
                           "challenge_id": rec.challenge_id, "version": rec.version,
                           "stars": rec.stars, "fuel_used": rec.fuel_used,
                           "level": 1})
        db.commit()
        return _submitted_response(rec, duplicated=False)


# ---------- 审核权限 / 历史兼容 ----------

def _index_columns(conn, table: str):
    """表上每个索引 → 其覆盖列（不含 rowid 伪列），用于识别旧的全局唯一索引。

    表名为模块内固定常量（非外部输入），PRAGMA 不支持参数绑定，直接拼接。
    """
    out = {}
    for row in conn.execute(
            f"PRAGMA index_list({table})").fetchall():
        idx = row[1]
        cols = [r[2] for r in conn.execute(
            f'PRAGMA index_info("{idx}")').fetchall()]
        out[idx] = cols
    return out


def _rebuild_scoped_unique(conn, table: str, scope_col: str,
                           constraint_name: str) -> bool:
    """把 (submission_id) 全局唯一约束重建为 (scope_col, submission_id) 复合唯一。

    旧库幂等键只有全局唯一：不同挑战/关卡复用同一键会把成绩串到别处。
    SQLite 不能直接改约束，按 12 步表重建流程保留全部数据与既有索引/外键。
    返回是否执行了重建。
    """
    indexes = _index_columns(conn, table)
    # 已是复合唯一的新库：无需迁移
    for cols in indexes.values():
        if cols == [scope_col, "submission_id"]:
            return False
    # 幂等键上没有单列唯一索引（非约束场景）也不处理
    if not any(cols == ["submission_id"] for cols in indexes.values()):
        return False

    cols_info = conn.execute(
        f"PRAGMA table_info({table})").fetchall()
    col_defs = [f"{r[1]} {r[2]}" + (" NOT NULL" if r[3] else "")
                + (f" DEFAULT {r[4]}" if r[4] is not None else "")
                + (" PRIMARY KEY" if r[5] else "")
                for r in cols_info]
    col_names = ", ".join(r[1] for r in cols_info)
    # 保留外键定义（仅 challenge_run / run_record 关联，按表名固定列出）
    fk = ""
    if table == "challenge_submission":
        fk = ", FOREIGN KEY(run_id) REFERENCES challenge_run (id)"
    elif table == "score_record":
        fk = ", FOREIGN KEY(run_id) REFERENCES run_record (id)"
    # 重建既有二级索引（submission_id 上的旧自动唯一索引除外）
    keep_index_names = []
    keep_indexes = []
    for idx, cols in indexes.items():
        if idx.startswith("sqlite_autoindex"):
            continue
        if cols == ["submission_id"]:
            continue
        keep_index_names.append(idx)
        keep_indexes.append(
            f'CREATE INDEX "{idx}" ON {table} ({", ".join(cols)})')

    conn.execute("PRAGMA foreign_keys=off")
    try:
        conn.execute("BEGIN")
        # RENAME 会把二级索引一并带到旧表名下且名字不变，先删除以避免重建时重名；
        # 自动唯一索引（submission_id 旧约束）随 DROP 旧表清除。
        for idx in keep_index_names:
            conn.execute(f'DROP INDEX "{idx}"')
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old_uk")
        conn.execute(
            f"CREATE TABLE {table} ({', '.join(col_defs)}, "
            f"CONSTRAINT {constraint_name} UNIQUE ({scope_col}, submission_id)"
            f"{fk})")
        conn.execute(
            f"INSERT INTO {table} ({col_names}) SELECT {col_names} "
            f"FROM {table}_old_uk")
        for stmt in keep_indexes:
            conn.execute(stmt)
        conn.execute(f"DROP TABLE {table}_old_uk")
        conn.execute("COMMIT")
    finally:
        conn.execute("PRAGMA foreign_keys=on")
    return True


def _sqlite_migrate(db) -> None:
    """对旧库做增量迁移（SQLite ALTER/表重建，全程幂等）。

    - challenge_submission 补 reviewed_by 列（历史库兼容）；
    - 多级审核：challenge_submission 补 review_level / review_stage_entered_at；
      challenge_version 补 review_pipeline_json；challenge_appeal 补 target_level；
    - 幂等键唯一约束由全局 (submission_id) 收窄为作用域复合唯一：
      challenge_submission(challenge_id, submission_id)、
      score_record(level_id, submission_id)，避免不同挑战/关卡复用同一键串成绩。
    """
    bind = db.get_bind()
    try:
        url = bind.url
    except Exception:
        url = None
    if url is None or not str(url).startswith("sqlite"):
        return
    path = url.database
    if not path or path == ":memory:":
        return
    conn = sqlite3.connect(path)
    changed = False
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "challenge_submission" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(challenge_submission)").fetchall()}
            if cols and "reviewed_by" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_submission ADD COLUMN reviewed_by VARCHAR(24)")
                conn.commit()
            if cols and "review_level" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_submission ADD COLUMN review_level INTEGER")
                conn.execute(
                    "ALTER TABLE challenge_submission "
                    "ADD COLUMN review_stage_entered_at FLOAT")
                conn.commit()
            changed |= _rebuild_scoped_unique(
                conn, "challenge_submission", "challenge_id",
                "uq_chsub_challenge_submission")
        if "challenge_version" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(challenge_version)").fetchall()}
            if cols and "review_pipeline_json" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_version "
                    "ADD COLUMN review_pipeline_json TEXT NOT NULL DEFAULT ''")
                conn.commit()
        if "challenge_appeal" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(challenge_appeal)").fetchall()}
            if cols and "target_level" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_appeal ADD COLUMN target_level INTEGER")
                conn.commit()
        if "score_record" in tables:
            changed |= _rebuild_scoped_unique(
                conn, "score_record", "level_id",
                "uq_score_level_submission")
        conn.commit()
    finally:
        conn.close()
    if changed:
        # 表重建后，池内已存在的连接仍持有旧表（DROP/RENAME 前）的 schema 缓存，
        # 让其全部失效；调用方的 session 也回到干净状态再重新取连接。
        db.rollback()
        bind.dispose()


def _seed_reviewers(db) -> None:
    """确保内置审核账号存在（同名不覆盖 token，避免破坏既有授权）。"""
    existing = {r.name: r for r in db.query(Reviewer).all()}
    added = False
    for spec in BUILTIN_REVIEWERS:
        if spec["name"] not in existing:
            db.add(Reviewer(name=spec["name"], token=spec["token"],
                            role=spec["role"], active=1))
            added = True
    if added:
        db.commit()


def _backfill_legacy_events(db) -> None:
    """兼容已审核历史成绩：为没有事件档案的已终审成绩补登 legacy 事件。"""
    fin = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.review_status.in_((APPROVED, REJECTED)))
             .all())
    if not fin:
        return
    with_events = {
        sid for (sid,) in db.query(ChallengeReviewEvent.submission_id).distinct().all()
    }
    n = 0
    for rec in fin:
        if rec.id in with_events:
            continue
        _add_event(db, rec, kind=EV_LEGACY,
                   actor=rec.reviewed_by or "历史审核",
                   actor_role="system",
                   detail={"status": rec.review_status,
                           "note": rec.review_note or "",
                           "reviewed_at": rec.reviewed_at})
        n += 1
    if n:
        db.commit()


_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False


def bootstrap(db) -> None:
    """启动引导：旧库加列迁移 + 内置审核账号 + 历史成绩事件补登（幂等）。

    不持有业务 _LOCK：迁移会打开第二个 SQLite 连接（ALTER TABLE），
    若在 _LOCK 临界区内、且主连接已开启写事务时执行会与写锁互锁。
    """
    global _BOOTSTRAPPED
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        _sqlite_migrate(db)
        _seed_reviewers(db)
        _backfill_legacy_events(db)
        _BOOTSTRAPPED = True


def reset_bootstrap_for_tests() -> None:
    """测试夹具用：换库后允许再次执行启动引导。"""
    global _BOOTSTRAPPED
    _BOOTSTRAPPED = False


def authenticate(db, token: Optional[str], *, required_role: Optional[str] = None):
    """校验审核凭证，返回 Reviewer；缺省 token 降级为内置初审员（本地单机开放入口）。

    required_role=moderator 时必须显式提供复核员 token：
    无 token/失效 → UnauthorizedReviewer(401)；初审员越权 → ForbiddenReviewer(403)。
    bootstrap 必须在调用方获取 _LOCK 之前完成（见 bootstrap 文档说明）。
    """
    bootstrap(db)
    if not token:
        if required_role == ROLE_MODERATOR:
            raise UnauthorizedReviewer("该操作需要复核员权限，请提供 X-Reviewer-Token")
        return db.query(Reviewer).filter(Reviewer.token == "local-reviewer").one()
    rv = (db.query(Reviewer)
            .filter(Reviewer.token == token, Reviewer.active == 1)
            .first())
    if rv is None:
        raise UnauthorizedReviewer("审核凭证无效或已停用")
    if required_role == ROLE_MODERATOR and rv.role != ROLE_MODERATOR:
        raise ForbiddenReviewer(f"「{rv.name}」是初审员，该操作需要复核员权限")
    return rv


def register_reviewer(db, *, token: str, name: str, role: str,
                      actor: Reviewer) -> dict:
    """复核员注册新的审核账号（仅 moderator 可调用）。"""
    name = _clean_text(name, 24, "审核员署名")
    token = _clean_text(token, 64, "审核令牌")
    if role not in (ROLE_REVIEWER, ROLE_MODERATOR):
        raise ValidationError(f"未知角色: {role}")
    rv = Reviewer(name=name, token=token, role=role, active=1, created_at=time.time())
    db.add(rv)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ReviewerConflict("审核员署名或令牌已存在")
    return {"id": rv.id, "name": rv.name, "role": rv.role}


def list_reviewers(db, *, active_only: bool = True) -> List[dict]:
    """审核账号名单（不含 token）：供设计者在审核级白名单中挑选指定审核人。"""
    bootstrap(db)
    q = db.query(Reviewer)
    if active_only:
        q = q.filter(Reviewer.active == 1)
    rows = q.order_by(Reviewer.role, Reviewer.id).all()
    return [{"id": r.id, "name": r.name, "role": r.role,
             "active": r.active, "created_at": r.created_at} for r in rows]


# ---------- 事件档案 ----------

def _add_event(db, rec, *, kind: str, actor: str = "",
               actor_role: str = "", detail: Optional[dict] = None,
               appeal=None) -> ChallengeReviewEvent:
    ev = ChallengeReviewEvent(
        submission_id=rec.id,
        appeal_id=appeal.id if appeal is not None else None,
        kind=kind, actor=(actor or "")[:24], actor_role=actor_role,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
        created_at=time.time())
    db.add(ev)
    return ev


# ---------- 排行榜/解锁联动效果（回滚可观测） ----------

def _snapshot_effects(db) -> dict:
    """操作前快照：各版本榜首 + 全部挑战解锁状态。"""
    version_best, player_best = _leaderboard_maps(db)
    builtin, ch_stars, total, titles = _unlock_context(db)
    unlocked = {}
    for ch in db.query(Challenge).filter(Challenge.status != "archived").all():
        ok, _ = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        unlocked[ch.id] = ok
    return {"version_best": version_best, "player_best": player_best,
            "unlocked": unlocked, "titles": titles}


def _effects(db, before: dict, rec: ChallengeSubmission, *,
             replayable_before: bool) -> dict:
    """汇总一条成绩操作对排行榜/回放/解锁的联动效果（供回滚审计）。"""
    version_best, player_best = _leaderboard_maps(db)
    version_top = version_best.get(rec.challenge_id, {}).get(rec.version)
    player_entry = (player_best.get(rec.challenge_id, {})
                    .get(rec.version, {}).get(rec.player))
    on_board = bool(
        player_entry and player_entry["record_id"] == rec.id
        and rec.review_status == APPROVED)
    builtin, ch_stars, total, titles = _unlock_context(db)
    unlocked_now, desc = {}, {}
    for ch in db.query(Challenge).filter(Challenge.status != "archived").all():
        ok, why = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        unlocked_now[ch.id] = ok
        desc[ch.id] = why
    changes = []
    for cid, was_unlocked in before["unlocked"].items():
        if was_unlocked != unlocked_now.get(cid):
            changes.append({
                "challenge_id": cid,
                "title": before["titles"].get(cid, f"#{cid}"),
                "unlocked": unlocked_now[cid],
                "unlock_desc": desc.get(cid),
            })
    return {
        "record_id": rec.id,
        "status": rec.review_status,
        "leaderboard": {
            "on_board": on_board,
            "best_record_id": version_top["record_id"] if version_top else None,
            "version": rec.version,
        },
        "replayable": {"before": replayable_before,
                       "after": rec.review_status == APPROVED},
        "unlock_changes": changes,
    }


# ---------- 审核（多级链路） ----------

def _pipeline_for_rec(db, rec: ChallengeSubmission) -> Tuple[ChallengeVersion, List[dict]]:
    """取成绩提交版本及其审核链（成绩锚定提交时版本，链路随之不可变）。"""
    ver = _get_version(db, rec.challenge_id, rec.version)
    return ver, pipeline_of(ver)


def _close_appeal(appeal: ChallengeAppeal, *, decision: str, note: str,
                  rv: Reviewer, now: float) -> None:
    appeal.status = decision
    appeal.decision_note = note
    appeal.decided_by = rv.name
    appeal.decided_at = now


def review(db, *, record_id: int, action: str, note: str = "",
           reviewer_token: Optional[str] = None,
           expect_level: Optional[int] = None) -> dict:
    """处理成绩在当前审核级队列中的裁决（多级审核状态机）。

    提交进入第 1 级 pending；通过且后面还有级别 → 进入下一级（review_advance，
    仍不上榜）；终审级通过 → approved（上榜/回放/解锁）；任一级驳回 → rejected。
    权限按提交版本该级配置校验：复核级仅 moderator；初审级的署名白名单非空时
    审核人须在名单内；moderator 可处理任意级（超时兜底）。
    申诉重审：被第 L 级驳回/撤销而发起的申诉回到第 L 级队列（appeal.target_level=L），
    在本接口裁决——维持(uphold 效果=驳回维持)、推翻(overturn 效果=通过并继续流转)；
    旧版默认链路（target_level 为空）仍须复核员走复核裁决接口。
    幂等：终审后重复同一终审动作 duplicated=True；相反动作 → ReviewConflict(409)。
    expect_level 供队列重试防串级：非当前级时 409。
    """
    if action not in _REVIEW_ACTIONS:
        raise ValidationError(f"未知审核动作: {action}")
    target = _REVIEW_ACTIONS[action]
    rv = authenticate(db, reviewer_token, required_role=ROLE_REVIEWER)
    with _LOCK:
        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
        ver, pipeline = _pipeline_for_rec(db, rec)
        level = _current_level(rec)
        stage = _authorize_stage(db, pipeline, level, rv)
        open_appeal = _open_appeal(db, rec.id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        clean_note = (note or "").strip()[:200]
        now = time.time()

        # 已终审：幂等 / 冲突
        if rec.review_status != PENDING:
            if rec.review_status == target:
                return _review_response(
                    rec, stage, pipeline, target, duplicated=True, actor=rv.name,
                    advanced=False, effects=_effects(
                        db, before, rec, replayable_before=replay_before))
            raise ReviewConflict(
                f"成绩已审核（{rec.review_status}），不能重复审核")

        # 旧版默认链路的申诉统一由复核员裁决接口处理，不进多级队列
        if open_appeal is not None and open_appeal.target_level is None:
            raise ReviewConflict(
                "成绩正在申诉复核中，请由复核员通过复核裁决（uphold/overturn）处理")
        if expect_level is not None and expect_level != level:
            raise ReviewConflict(
                f"该成绩当前在第 {level} 级队列（请求第 {expect_level} 级），请刷新队列")

        is_final = level >= len(pipeline)
        if target == REJECTED:
            # 维持驳回（含申诉维持原判）：成绩终止于 rejected，停留在驳回级
            rec.review_status = REJECTED
            rec.review_stage_entered_at = None
            rec.review_note = clean_note
            rec.reviewed_by = rv.name
            rec.reviewed_at = now
            if open_appeal is not None:
                _close_appeal(open_appeal, decision=APPEAL_UPHOLD,
                              note=clean_note, rv=rv, now=now)
                _add_event(db, rec, kind=EV_APPEAL_DECISION[APPEAL_UPHOLD],
                           actor=rv.name, actor_role=rv.role, appeal=open_appeal,
                           detail={"round": open_appeal.round,
                                   "from_status": open_appeal.from_status,
                                   "level": level, "stage": stage["name"],
                                   "note": clean_note})
            _add_event(db, rec, kind=EV_REVIEW[REJECTED], actor=rv.name,
                       actor_role=rv.role, appeal=open_appeal,
                       detail={"level": level, "stage": stage["name"],
                               "note": clean_note})
            db.commit()
            return _review_response(
                rec, stage, pipeline, REJECTED, duplicated=False, actor=rv.name,
                advanced=False, appeal=open_appeal,
                effects=_effects(db, before, rec, replayable_before=replay_before))

        # target == approved：先落“通过当前级”的申诉裁决（若有）
        if open_appeal is not None:
            _close_appeal(open_appeal, decision=APPEAL_OVERTURN,
                          note=clean_note, rv=rv, now=now)
            _add_event(db, rec, kind=EV_APPEAL_DECISION[APPEAL_OVERTURN],
                       actor=rv.name, actor_role=rv.role, appeal=open_appeal,
                       detail={"round": open_appeal.round,
                               "from_status": open_appeal.from_status,
                               "level": level, "stage": stage["name"],
                               "note": clean_note})
        rec.review_note = clean_note
        if not is_final:
            # 逐级通过：进入下一级队列（不上榜/不回放/不解锁）
            next_stage = pipeline[level]  # 0 基下标：当前 level 的下一项
            rec.review_level = level + 1
            rec.review_stage_entered_at = now
            _add_event(db, rec, kind=EV_ADVANCE, actor=rv.name,
                       actor_role=rv.role, appeal=open_appeal,
                       detail={"from_level": level, "from_stage": stage["name"],
                               "to_level": level + 1,
                               "to_stage": next_stage["name"],
                               "note": clean_note})
            db.commit()
            return _review_response(
                rec, next_stage, pipeline, PENDING, duplicated=False,
                actor=rv.name, advanced=True, appeal=open_appeal,
                effects=_effects(db, before, rec, replayable_before=replay_before))

        # 终审级通过：approved，联动排行榜/回放/解锁
        rec.review_status = APPROVED
        rec.review_level = level
        rec.review_stage_entered_at = None
        rec.reviewed_by = rv.name
        rec.reviewed_at = now
        _add_event(db, rec, kind=EV_REVIEW[APPROVED], actor=rv.name,
                   actor_role=rv.role, appeal=open_appeal,
                   detail={"level": level, "stage": stage["name"],
                           "total_levels": len(pipeline), "note": clean_note})
        db.commit()
        return _review_response(
            rec, stage, pipeline, APPROVED, duplicated=False, actor=rv.name,
            advanced=False, appeal=open_appeal,
            effects=_effects(db, before, rec, replayable_before=replay_before))


def _review_response(rec, stage, pipeline, status, *, duplicated: bool, actor: str,
                     advanced: bool, effects: Optional[dict] = None,
                     appeal=None) -> dict:
    stage_view = _stage_view(stage,
                             rec.review_stage_entered_at
                             if status == PENDING else None,
                             time.time())
    cur_level = _current_level(rec) if status == PENDING else rec.review_level
    stage_view["final_level"] = cur_level >= len(pipeline)
    return {"record_id": rec.id, "challenge_id": rec.challenge_id,
            "version": rec.version, "review_status": status,
            "reviewed_by": actor, "duplicated": duplicated,
            "advanced": advanced,
            "review_level": cur_level,
            "total_levels": len(pipeline),
            "stage": stage_view,
            "appeal_round": appeal.round if appeal is not None else None,
            "effects": effects}


def revoke(db, *, record_id: int, note: str = "",
           moderator_token: Optional[str] = None) -> dict:
    """复核员撤销已上榜成绩：approved → revoked。

    自动回滚：立即移出排行榜、关闭轨迹回放，并级联回收该成绩解锁的挑战
    （解锁状态实时求值，后续有其他成绩补上时会自动重新开放）。
    幂等：重复撤销返回 duplicated=True；非 approved 状态 → ReviewConflict(409)。
    """
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        rec = _get_submission(db, record_id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == REVOKED:
            return _moderation_response(rec, EV_REVOKE, duplicated=True,
                                        actor=rv.name,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != APPROVED:
            raise ReviewConflict(
                f"只有已通过的成绩可以撤销（当前 {rec.review_status}）")
        if _open_appeal(db, rec.id) is not None:
            raise ReviewConflict("成绩正在申诉复核中，不能撤销")
        _, pipeline = _pipeline_for_rec(db, rec)
        _reject_terminal(db, rec, status=REVOKED, note=note, reviewer=rv,
                         kind=EV_REVOKE, pipeline=pipeline)
        db.commit()
        return _moderation_response(rec, EV_REVOKE, duplicated=False,
                                    actor=rv.name,
                                    effects=_effects(db, before, rec,
                                                     replayable_before=replay_before))


def restore(db, *, record_id: int, note: str = "",
            moderator_token: Optional[str] = None) -> dict:
    """复核员恢复被撤销的成绩：revoked → approved（重新上榜/回放/解锁）。

    幂等：已 approved 返回 duplicated=True；其他状态 → ReviewConflict(409)。
    """
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        rec = _get_submission(db, record_id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == APPROVED:
            return _moderation_response(rec, EV_RESTORE, duplicated=True,
                                        actor=rv.name,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != REVOKED:
            raise ReviewConflict(
                f"只有已撤销的成绩可以恢复（当前 {rec.review_status}）")
        _, pipeline = _pipeline_for_rec(db, rec)
        final_level = len(pipeline)
        rec.review_status = APPROVED
        rec.review_level = final_level
        rec.review_stage_entered_at = None
        rec.review_note = (note or "").strip()[:200]
        rec.reviewed_by = rv.name
        rec.reviewed_at = time.time()
        stage = _stage_at(pipeline, final_level)
        _add_event(db, rec, kind=EV_RESTORE, actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"note": rec.review_note, "level": final_level,
                           "stage": stage["name"]})
        db.commit()
        return _moderation_response(rec, EV_RESTORE, duplicated=False,
                                    actor=rv.name,
                                    effects=_effects(db, before, rec,
                                                     replayable_before=replay_before))


def _moderation_response(rec, action_kind, *, duplicated: bool, actor: str,
                         effects: dict) -> dict:
    return {"record_id": rec.id, "challenge_id": rec.challenge_id,
            "version": rec.version, "action": action_kind,
            "review_status": rec.review_status,
            "reviewed_by": actor, "duplicated": duplicated,
            "effects": effects}


def _reject_terminal(db, rec, *, status: str, note: str, reviewer: Reviewer,
                     kind: str, pipeline: Optional[List[dict]] = None) -> None:
    """撤销上榜成绩：终止于 revoked，停留在终审级（其申诉将路由回终审级）。"""
    rec.review_status = status
    rec.review_note = (note or "").strip()[:200]
    rec.reviewed_by = reviewer.name
    rec.reviewed_at = time.time()
    detail = {"note": rec.review_note}
    if pipeline is not None:
        rec.review_level = len(pipeline)
        rec.review_stage_entered_at = None
        stage = _stage_at(pipeline, len(pipeline))
        detail.update(level=len(pipeline), stage=stage["name"])
    _add_event(db, rec, kind=kind, actor=reviewer.name,
               actor_role=ROLE_MODERATOR, detail=detail)


def _get_submission(db, record_id: int) -> ChallengeSubmission:
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
    return rec


def _open_appeal(db, record_id: int) -> Optional[ChallengeAppeal]:
    return (db.query(ChallengeAppeal)
              .filter(ChallengeAppeal.submission_id == record_id,
                      ChallengeAppeal.status == APPEAL_PENDING)
              .first())


# ---------- 玩家申诉 ----------

def create_appeal(db, *, record_id: int, player: str, reason: str,
                  appeal_id: Optional[str]) -> dict:
    """玩家对驳回/撤销的成绩发起申诉（幂等：appeal_id 去重）。

    约束：署名须与提交者一致（仅本人可申诉）；仅 rejected/revoked 可申诉；
    每条成绩最多 MAX_APPEAL_ROUNDS 轮，同时仅一条待裁决申诉。
    受理后成绩回到 pending 进入“正确的队列”：
    - 提交版本配置了多级审核链：回到做出驳回/撤销的那一级（rejected 停留在
      驳回级 review_level；revoked 是终审后撤销，回到终审级），由该级指定
      审核人在普通审核接口裁决（appeal.target_level = 级别）；
    - 旧版默认单级链路：target_level 为空，统一进复核员队列走复核裁决接口。
    """
    player = (player or "").strip()[:24] or DEFAULT_PLAYER
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("申诉理由不能为空")
    if len(reason) > MAX_APPEAL_REASON:
        raise ValidationError(f"申诉理由过长（≤{MAX_APPEAL_REASON} 字）")
    with _LOCK:
        # 幂等重放：同一申诉已受理 → 返回首个结果
        if appeal_id:
            dup = (db.query(ChallengeAppeal)
                     .filter(ChallengeAppeal.appeal_uid == appeal_id)
                     .first())
            if dup is not None:
                return _appeal_response(dup, duplicated=True)
        rec = _get_submission(db, record_id)
        if rec.player != player:
            raise ForbiddenReviewer("只有提交该成绩的玩家本人可以申诉")
        if rec.review_status not in (REJECTED, REVOKED):
            raise AppealConflict(
                f"当前状态（{rec.review_status}）不能申诉，仅驳回/撤销的成绩可申诉")
        if _open_appeal(db, rec.id) is not None:
            raise AppealConflict("该成绩已有进行中的申诉，请等待裁决")
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == rec.id)
                    .count())
        if rounds >= MAX_APPEAL_ROUNDS:
            raise AppealConflict(f"每条成绩最多申诉 {MAX_APPEAL_ROUNDS} 次")
        # 路由到正确队列：未配置审核链的旧版本（legacy）走复核员队列；
        # 配置过的多级链路回到原判级别——驳回停留级 / 撤销回到终审级。
        ver, pipeline = _pipeline_for_rec(db, rec)
        legacy = bool(pipeline[0].get("legacy"))
        if legacy:
            target_level = None
        elif rec.review_status == REVOKED:
            target_level = len(pipeline)
        else:
            target_level = _current_level(rec)
        now = time.time()
        appeal = ChallengeAppeal(
            appeal_uid=appeal_id or uuid.uuid4().hex,
            submission_id=rec.id, round=rounds + 1,
            from_status=rec.review_status, player=player,
            target_level=target_level,
            reason=reason, status=APPEAL_PENDING,
            created_at=now)
        db.add(appeal)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeAppeal)
                     .filter(ChallengeAppeal.appeal_uid == appeal.appeal_uid)
                     .first())
            return _appeal_response(dup, duplicated=True)
        # 成绩回到对应审核级待审队列；排行榜/回放/解锁因状态变化自动回滚
        rec.review_status = PENDING
        if target_level is not None:
            rec.review_level = target_level
            rec.review_stage_entered_at = now
        stage = _stage_at(pipeline, target_level)
        _add_event(db, rec, kind=EV_APPEAL, actor=player, actor_role="player",
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "target_level": target_level,
                           "target_stage": stage["name"]
                               if target_level is not None else None,
                           "reason": reason}, appeal=appeal)
        db.commit()
        return _appeal_response(appeal, duplicated=False)


def decide_appeal(db, *, appeal_id_key: int, decision: str, note: str = "",
                  moderator_token: Optional[str] = None) -> dict:
    """复核员裁决申诉（旧版默认单级链路）：uphold=维持原判（成绩回到 from_status）；
    overturn=推翻原判（改判通过 approved，重新上榜/回放/解锁）。

    多级审核链版本的申诉已路由到对应审核级队列（appeal.target_level 非空），
    须由该级指定审核人在普通审核接口处理，此处返回 ReviewConflict(409)。
    幂等：对已裁决申诉重复同一裁决返回 duplicated=True；
    相反裁决 → ReviewConflict(409)。
    """
    if decision not in (APPEAL_UPHOLD, APPEAL_OVERTURN):
        raise ValidationError(f"未知复核裁决: {decision}（uphold/overturn）")
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        appeal = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.id == appeal_id_key)
                    .first())
        if appeal is None:
            raise AppealNotFound(f"申诉记录不存在: {appeal_id_key}")
        rec = _get_submission(db, appeal.submission_id)
        if appeal.target_level is not None:
            _, pipeline = _pipeline_for_rec(db, rec)
            stage = _stage_at(pipeline, appeal.target_level)
            raise ReviewConflict(
                f"该申诉已路由到第 {appeal.target_level} 级「{stage['name']}」"
                "审核队列，请由该级审核人在审核队列中处理")
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if appeal.status != APPEAL_PENDING:
            decided = "overturn" if appeal.status == APPEAL_OVERTURN else "uphold"
            if decided == decision:
                return _appeal_decision_response(
                    appeal, rec, decision, duplicated=True, actor=rv.name,
                    effects=_effects(db, before, rec,
                                     replayable_before=replay_before))
            raise ReviewConflict(
                f"申诉已裁决（{decided}），不能改判")
        appeal.status = decision
        appeal.decision_note = (note or "").strip()[:200]
        appeal.decided_by = rv.name
        appeal.decided_at = time.time()
        rec.review_status = (
            APPROVED if decision == APPEAL_OVERTURN else appeal.from_status)
        rec.review_note = appeal.decision_note
        rec.reviewed_by = rv.name
        rec.reviewed_at = time.time()
        _add_event(db, rec, kind=EV_APPEAL_DECISION[decision], actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "note": appeal.decision_note},
                   appeal=appeal)
        db.commit()
        return _appeal_decision_response(
            appeal, rec, decision, duplicated=False, actor=rv.name,
            effects=_effects(db, before, rec,
                             replayable_before=replay_before))


def _appeal_response(ap: ChallengeAppeal, *, duplicated: bool) -> dict:
    return {"saved": True, "duplicated": duplicated,
            "appeal_id": ap.id, "appeal_uid": ap.appeal_uid,
            "record_id": ap.submission_id, "round": ap.round,
            "from_status": ap.from_status, "status": ap.status,
            "target_level": ap.target_level,
            "review_status": PENDING,
            "review_level": ap.target_level or 1}


def _appeal_decision_response(ap, rec, decision, *, duplicated: bool,
                              actor: str, effects: dict) -> dict:
    return {"appeal_id": ap.id, "record_id": rec.id,
            "challenge_id": rec.challenge_id, "version": rec.version,
            "decision": decision, "status": decision,
            "review_status": rec.review_status,
            "reviewed_by": actor, "round": ap.round,
            "duplicated": duplicated, "effects": effects}


def list_appeals(db, *, status: str = APPEAL_PENDING,
                 level: Optional[int] = None, limit: int = 50) -> List[dict]:
    """申诉队列：默认待裁决。

    level=None：仅返回旧版默认链路、由复核员统一裁决的申诉（复核员工作流）；
    level>=1：返回路由到该审核级队列的多级链路申诉（由该级审核人处理）。
    """
    q = db.query(ChallengeAppeal)
    if status in (APPEAL_PENDING, APPEAL_UPHOLD, APPEAL_OVERTURN):
        q = q.filter(ChallengeAppeal.status == status)
    if level is None:
        q = q.filter(ChallengeAppeal.target_level.is_(None))
    else:
        q = q.filter(ChallengeAppeal.target_level == level)
    rows = (q.order_by(ChallengeAppeal.id.desc())
             .limit(max(1, min(200, limit))).all())
    sub_ids = {a.submission_id for a in rows}
    subs = {s.id: s for s in
            (db.query(ChallengeSubmission)
               .filter(ChallengeSubmission.id.in_(sub_ids)).all())} if sub_ids else {}
    versions = {(v.challenge_id, v.version): v
                for v in db.query(ChallengeVersion).all()}
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    out = []
    for a in rows:
        s = subs.get(a.submission_id)
        ver = versions.get((s.challenge_id, s.version)) if s else None
        pipeline = pipeline_of(ver) if ver else _legacy_pipeline()
        stage = _stage_at(pipeline, a.target_level or 1)
        out.append({
            "appeal_id": a.id, "record_id": a.submission_id,
            "challenge_id": s.challenge_id if s else None,
            "challenge_title": titles.get(s.challenge_id, f"#{s.challenge_id}")
                               if s else "",
            "version": s.version if s else None,
            "player": a.player, "round": a.round,
            "from_status": a.from_status, "status": a.status,
            "target_level": a.target_level,
            "target_stage": stage["name"] if a.target_level else None,
            "stage_role": stage["role"] if a.target_level else ROLE_MODERATOR,
            "reason": a.reason, "decision_note": a.decision_note,
            "decided_by": a.decided_by,
            "stars": s.stars if s else None,
            "fuel_used": s.fuel_used if s else None,
            "elapsed_days": s.elapsed_days if s else None,
            "created_at": a.created_at, "decided_at": a.decided_at,
        })
    return out


def player_submissions(db, player: str, *, challenge_id: Optional[int] = None,
                       version: Optional[int] = None,
                       limit: int = 50) -> List[dict]:
    """按飞行员署名查询本人提交（玩家查看审核状态/入口申诉用）。"""
    player = (player or "").strip()[:24] or DEFAULT_PLAYER
    q = db.query(ChallengeSubmission).filter(ChallengeSubmission.player == player)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    if version is not None:
        q = q.filter(ChallengeSubmission.version == version)
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(200, limit))).all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    versions = {(v.challenge_id, v.version): v
                for v in db.query(ChallengeVersion).all()}
    out = []
    for r in rows:
        ap = _open_appeal(db, r.id)
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == r.id).count())
        ver = versions.get((r.challenge_id, r.version))
        pipeline = pipeline_of(ver) if ver else _legacy_pipeline()
        stage = _stage_decision_view(db, r, pipeline) if r.review_status == PENDING \
            else None
        out.append({
            "record_id": r.id, "challenge_id": r.challenge_id,
            "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
            "version": r.version, "stars": r.stars,
            "fuel_used": r.fuel_used, "elapsed_days": r.elapsed_days,
            "review_status": r.review_status, "review_note": r.review_note,
            "reviewed_by": r.reviewed_by,
            "review_level": _current_level(r),
            "total_levels": len(pipeline),
            "stage": stage,
            "appeal_rounds": rounds,
            "appeal_open": ap.id if ap else None,
            "appeal_target_level": ap.target_level if ap else None,
            "appeal_available": (
                r.review_status in (REJECTED, REVOKED)
                and ap is None and rounds < MAX_APPEAL_ROUNDS),
            "replayable": r.review_status == APPROVED,
            "created_at": r.created_at,
        })
    return out


# ---------- 可追溯时间线 ----------

def submission_timeline(db, record_id: int) -> Optional[dict]:
    """成绩全链路：提交 → 初审 → 申诉 → 复核 → 撤销/恢复（事件档案顺序回放）。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    ch = db.query(Challenge).filter(Challenge.id == rec.challenge_id).first()
    ver = (db.query(ChallengeVersion)
             .filter(ChallengeVersion.challenge_id == rec.challenge_id,
                     ChallengeVersion.version == rec.version)
             .first())
    pipeline = pipeline_of(ver) if ver else _legacy_pipeline()
    events = (db.query(ChallengeReviewEvent)
                .filter(ChallengeReviewEvent.submission_id == rec.id)
                .order_by(ChallengeReviewEvent.id)
                .all())
    appeals = (db.query(ChallengeAppeal)
                 .filter(ChallengeAppeal.submission_id == rec.id)
                 .order_by(ChallengeAppeal.id)
                 .all())
    return {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "challenge_title": ch.title if ch else f"#{rec.challenge_id}",
        "version": rec.version, "player": rec.player,
        "stars": rec.stars, "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "review_pipeline": _pipeline_summary(pipeline),
        "review_level": _current_level(rec),
        "review_status": rec.review_status, "review_note": rec.review_note,
        "reviewed_by": rec.reviewed_by,
        "appeal_rounds": len(appeals),
        "appeal_open": _open_appeal(db, rec.id) is not None,
        "appeal_available": (
            rec.review_status in (REJECTED, REVOKED)
            and _open_appeal(db, rec.id) is None
            and len(appeals) < MAX_APPEAL_ROUNDS),
        "replayable": rec.review_status == APPROVED,
        "created_at": rec.created_at,
        "events": [{
            "seq": i + 1, "kind": e.kind, "actor": e.actor,
            "actor_role": e.actor_role, "appeal_id": e.appeal_id,
            "detail": json.loads(e.detail_json),
            "created_at": e.created_at,
        } for i, e in enumerate(events)],
        "appeals": [{
            "appeal_id": a.id, "round": a.round, "from_status": a.from_status,
            "target_level": a.target_level,
            "target_stage": (_stage_at(pipeline, a.target_level)["name"]
                             if a.target_level else None),
            "reason": a.reason, "status": a.status,
            "decision_note": a.decision_note, "decided_by": a.decided_by,
            "created_at": a.created_at, "decided_at": a.decided_at,
        } for a in appeals],
    }


def list_submissions(db, *, challenge_id: Optional[int] = None,
                     status: str = "pending", limit: int = 50,
                     level: Optional[int] = None,
                     overdue: Optional[bool] = None) -> List[dict]:
    """成绩提交列表：默认待审核队列（含申诉重审，附 appeal 标记），可按挑战/级过滤。

    pending 状态下可按 level（当前所处审核级，旧版归一为第 1 级）与
    overdue（该级 SLA 是否超时）过滤，供各级审核人查看自己的队列。
    """
    q = db.query(ChallengeSubmission)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    if status in ("pending", "approved", "rejected", "revoked"):
        q = q.filter(ChallengeSubmission.review_status == status)
    if status == "pending" and level is not None:
        q = q.filter(
            # 旧存档空级别归一到第 1 级
            (ChallengeSubmission.review_level == level)
            | ((ChallengeSubmission.review_level.is_(None)) & (level == 1)))
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(500, limit)))
             .all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    versions = {(v.challenge_id, v.version): v
                for v in db.query(ChallengeVersion).all()}
    result = []
    for r in rows:
        ap = _open_appeal(db, r.id)
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == r.id).count())
        ver = versions.get((r.challenge_id, r.version))
        pipeline = pipeline_of(ver) if ver else _legacy_pipeline()
        cur_level = _current_level(r)
        stage = (_stage_decision_view(db, r, pipeline)
                 if r.review_status == PENDING else None)
        if overdue is not None and (stage is None or stage["overdue"] != overdue):
            continue
        result.append({
            "record_id": r.id,
            "challenge_id": r.challenge_id,
            "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
            "version": r.version,
            "player": r.player,
            "stars": r.stars,
            "fuel_used": r.fuel_used,
            "elapsed_days": r.elapsed_days,
            "review_status": r.review_status,
            "review_note": r.review_note,
            "reviewed_by": r.reviewed_by,
            "review_level": cur_level,
            "total_levels": len(pipeline),
            "stage": stage,
            "appeal_open": ap.id if ap else None,
            "appeal_round": ap.round if ap else None,
            "appeal_target_level": ap.target_level if ap else None,
            "appeal_routed": bool(ap and ap.target_level is not None),
            "appeal_legacy": bool(ap and ap.target_level is None),
            "appeal_reason": ap.reason if ap else None,
            "appeal_rounds_total": rounds,
            "created_at": r.created_at,
        })
    return result[:max(1, min(200, limit))]


# ---------- 排行榜与回放 ----------

def leaderboard(db, challenge_id: int, version: Optional[int] = None,
                limit: int = 50) -> dict:
    """排行榜：仅审核通过的成绩，按版本结算，每名玩家取最佳一条。

    排序：星数 → 燃料 → 用时 → 先到优先。
    """
    ch = _get_challenge(db, challenge_id)
    v_no = version or ch.current_version
    _get_version(db, challenge_id, v_no)  # 版本必须存在
    version_count = (db.query(ChallengeVersion)
                       .filter(ChallengeVersion.challenge_id == challenge_id)
                       .count())
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.challenge_id == challenge_id,
                      ChallengeSubmission.version == v_no,
                      ChallengeSubmission.review_status == "approved")
              .order_by(ChallengeSubmission.stars.desc(),
                        ChallengeSubmission.fuel_used.asc(),
                        ChallengeSubmission.elapsed_days.asc(),
                        ChallengeSubmission.id.asc())
              .all())
    entries, seen = [], set()
    for r in rows:
        if r.player in seen:
            continue
        seen.add(r.player)
        entries.append({
            "rank": len(entries) + 1,
            "record_id": r.id,
            "version": r.version,
            "player": r.player,
            "stars": r.stars,
            "fuel_used": r.fuel_used,
            "elapsed_days": r.elapsed_days,
            "replayable": True,
            "created_at": r.created_at,
        })
        if len(entries) >= max(1, min(100, limit)):
            break
    return {"challenge_id": challenge_id, "version": v_no,
            "current_version": ch.current_version,
            "version_count": version_count,
            "entries": entries}


def submission_detail(db, record_id: int) -> Optional[dict]:
    """成绩详情：审核通过后附带动作方案与轨迹（供回放）；否则只回元信息。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    open_ap = _open_appeal(db, rec.id)
    rounds = (db.query(ChallengeAppeal)
                .filter(ChallengeAppeal.submission_id == rec.id).count())
    ver = (db.query(ChallengeVersion)
             .filter(ChallengeVersion.challenge_id == rec.challenge_id,
                     ChallengeVersion.version == rec.version)
             .first())
    pipeline = pipeline_of(ver) if ver else _legacy_pipeline()
    detail = {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "player": rec.player,
        "stars": rec.stars,
        "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "review_status": rec.review_status,
        "review_note": rec.review_note,
        "reviewed_by": rec.reviewed_by,
        "review_level": _current_level(rec),
        "total_levels": len(pipeline),
        "stage": (_stage_decision_view(db, rec, pipeline)
                  if rec.review_status == PENDING else None),
        "review_pipeline": _pipeline_summary(pipeline),
        "appeal_rounds": rounds,
        "appeal_open": open_ap.id if open_ap else None,
        "appeal_target_level": open_ap.target_level if open_ap else None,
        "appeal_available": (
            rec.review_status in (REJECTED, REVOKED)
            and open_ap is None and rounds < MAX_APPEAL_ROUNDS),
        "replayable": rec.review_status == "approved",
        "created_at": rec.created_at,
    }
    if detail["replayable"]:
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.id == rec.run_id)
                 .first())
        if run is not None:
            detail.update({
                "run_uid": run.run_uid,
                "ok": bool(run.ok),
                "reason": run.reason,
                "actions": json.loads(run.actions_json),
                "trajectory": json.loads(run.trajectory_json),
                "events": json.loads(run.events_json),
                "milestones": json.loads(run.milestones_json),
            })
    return detail
