"""社区航线挑战服务：版本化发布 / 幂等提交 / 按版本多级审核 / 申诉队列路由 / 排行榜 / 回放 / 解锁联动。

设计要点：
- 版本化：每次发布生成不可变的 ChallengeVersion（预算 + 里程碑 + 时间限制 +
  审核流 review_flow），飞行记录与成绩都锚定具体版本；排行榜按版本结算，
  旧版本仍可查看与回放，其审核流也随之固化、不可修改。
- 多级审核流：review_flow 含 1~5 个审核级（名称/角色 reviewer|moderator/
  指定审核人名单/SLA 时限）与申诉去向（appeal_route：moderator=复核员裁决、
  stage=回到驳回级重审）。提交进第 1 级，逐级通过，末级通过才 approved；
  任意一级 reject 即落选。未配置的版本（含全部历史版本）降级为默认单级流，
  行为与旧版一致。
- 申诉队列路由：route=moderator 的申诉进复核员队列由其 uphold/overturn；
  route=stage 的申诉把成绩重置到驳回级（SLA 重新计时），由该级审核人在
  审核接口裁决——该级通过=推翻驳回并继续向后流转（可能还有后续级），
  再驳回=维持原判。revoked（上榜后撤销）始终由复核员受理。
- 幂等结算：/run 落库执行档案（ChallengeRun），/submit 以 submission_id 为幂等键
  关联档案落库成绩；重复提交（双击/重试/多标签页）返回首个结果，不重复计数。
- 审核联动：仅末级通过（approved）的成绩才进入排行榜、开放轨迹回放，并计入
  解锁条件。解锁状态按规则实时求值，审核通过即自动联动，无需额外迁移。
- 撤销/恢复：复核员可 revoke 上榜成绩（出榜、关回放、级联回收解锁）或
  restore（重新上榜/回放/解锁）；接口返回前后 diff 的回滚效果，均幂等。
- 可追溯：每次状态迁移追加一条不可变的 ChallengeReviewEvent（提交/逐级
  通过驳回/申诉/裁决/撤销/恢复，事件详情带审核级名称与级次）；历史已审核
  成绩在启动时补登 legacy 事件，旧库 pending 成绩补齐当前级与 SLA 起算时间。
- 审核权限：Reviewer 分 reviewer（初审）/moderator（复核、撤销、注册）两级，
  token 经 X-Reviewer-Token 上送；审核级可指定名单，名单为空则该角色全员
  可处理；内置本地账号开箱即用，缺省调用降级为内置初审员。
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
EV_APPEAL = "appeal"
EV_APPEAL_DECISION = {APPEAL_UPHOLD: "appeal_uphold", APPEAL_OVERTURN: "appeal_overturn"}
EV_REVOKE = "revoke"
EV_RESTORE = "restore"
EV_LEGACY = "legacy"

ROLE_REVIEWER = "reviewer"
ROLE_MODERATOR = "moderator"
MAX_APPEAL_ROUNDS = 2
MAX_APPEAL_REASON = 500

# 多级审核流
MAX_REVIEW_STAGES = 5
STAGE_NAME_MAX = 24
SLA_MIN_HOURS, SLA_MAX_HOURS = 0.5, 24 * 30
# 申诉去向：moderator=复核员队列裁决（默认）；stage=回到驳回时的审核级重审
APPEAL_ROUTE_MODERATOR = "moderator"
APPEAL_ROUTE_STAGE = "stage"

# 内置审核账号（本地单机开箱即用；正式部署可禁用/改 token）
BUILTIN_REVIEWERS = [
    {"name": "本机初审员", "token": "local-reviewer", "role": ROLE_REVIEWER},
    {"name": "本机复核员", "token": "local-moderator", "role": ROLE_MODERATOR},
]


def default_review_flow() -> dict:
    """缺省审核流：单级初审（所有初审员可处理）+ 复核员受理申诉。

    旧版本与未配置审核流的版本统一按此降级，历史行为完全不变。
    """
    return {"stages": [{"name": "初审", "role": ROLE_REVIEWER,
                        "reviewers": [], "sla_hours": None}],
            "appeal_route": APPEAL_ROUTE_MODERATOR}


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


class StageConflict(RuntimeError):
    """审核级不匹配（成绩当前不在该审核级，或该级不由该队列处理）。"""


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


def validate_review_flow(db, flow) -> Optional[dict]:
    """校验按版本配置的多级审核流；None / 空 dict 表示用默认流（返回 None）。

    结构：{"stages": [{"name", "role", "reviewers", "sla_hours"}],
           "appeal_route": "moderator" | "stage"}
    - 1~MAX_REVIEW_STAGES 个审核级，名称不可重复；
    - role 限定 reviewer/moderator；reviewers 为显式指定审核人署名（可空=
      该角色全员可处理），署名必须是已注册且角色匹配、未停用的账号；
    - sla_hours 为该级审核时限（小时，可空=不限时），仅用于超时提示；
    - appeal_route=stage 时申诉回到驳回时的审核级，由该级指定审核人裁决。
    """
    if not flow:
        return None
    if not isinstance(flow, dict):
        raise ValidationError("审核流配置必须是对象")
    stages_in = flow.get("stages")
    if not isinstance(stages_in, list) or not stages_in:
        raise ValidationError("审核流至少配置 1 个审核级")
    if len(stages_in) > MAX_REVIEW_STAGES:
        raise ValidationError(f"审核级最多 {MAX_REVIEW_STAGES} 级")
    known = {r.name: r for r in db.query(Reviewer).all()}
    stages, names = [], set()
    for i, st in enumerate(stages_in, 1):
        st = st or {}
        name = _clean_text(st.get("name"), STAGE_NAME_MAX, f"第 {i} 级名称")
        if name in names:
            raise ValidationError(f"审核级名称重复: {name}")
        names.add(name)
        role = st.get("role") or ROLE_REVIEWER
        if role not in (ROLE_REVIEWER, ROLE_MODERATOR):
            raise ValidationError(f"第 {i} 级角色未知: {role}")
        raw_reviewers = st.get("reviewers") or []
        if not isinstance(raw_reviewers, list):
            raise ValidationError(f"第 {i} 级审核人必须是署名列表")
        reviewers = []
        for rn in raw_reviewers:
            rn = (rn or "").strip()
            if not rn:
                continue
            if len(rn) > 24:
                raise ValidationError(f"第 {i} 级审核人署名过长: {rn}")
            acc = known.get(rn)
            if acc is None or not acc.active:
                raise ValidationError(f"第 {i} 级指定的审核人不存在或已停用: {rn}")
            if acc.role != role:
                raise ValidationError(
                    f"审核人「{rn}」是{'复核员' if acc.role == ROLE_MODERATOR else '初审员'}"
                    f"，与第 {i} 级要求的角色不匹配")
            if rn not in reviewers:
                reviewers.append(rn)
        sla = st.get("sla_hours")
        if sla is not None and sla != "":
            try:
                sla = float(sla)
            except (TypeError, ValueError):
                raise ValidationError(f"第 {i} 级审核时限必须是数字（小时）")
            if not SLA_MIN_HOURS <= sla <= SLA_MAX_HOURS:
                raise ValidationError(
                    f"第 {i} 级审核时限需在 {SLA_MIN_HOURS}~{SLA_MAX_HOURS} 小时之间")
        else:
            sla = None
        stages.append({"name": name, "role": role,
                       "reviewers": reviewers, "sla_hours": sla})
    route = flow.get("appeal_route") or APPEAL_ROUTE_MODERATOR
    if route not in (APPEAL_ROUTE_MODERATOR, APPEAL_ROUTE_STAGE):
        raise ValidationError(f"未知申诉去向: {route}")
    return {"stages": stages, "appeal_route": route}


# ---------- 发布（版本化） ----------

def _add_version(db, challenge_id: int, version: int, defn: dict,
                 review_flow: Optional[dict] = None) -> ChallengeVersion:
    ver = ChallengeVersion(
        challenge_id=challenge_id, version=version,
        name=defn["name"], brief=defn["brief"], hint=defn["hint"],
        budget_dv=defn["budget_dv"], t_max=defn["t_max"],
        milestones_json=json.dumps(defn["milestones"], ensure_ascii=False),
        review_flow_json=json.dumps(review_flow, ensure_ascii=False)
                             if review_flow else "",
        created_at=time.time(),
    )
    db.add(ver)
    return ver


def create_challenge(db, *, title: str, author: str, definition: dict,
                     unlock_rule=None, review_flow=None) -> dict:
    """发布新挑战：创建挑战主体 + 不可变的 v1 版本（含多级审核流配置）。"""
    title = _clean_text(title, 40, "挑战标题")
    author = _clean_text(author, 24, "设计者署名", allow_empty=True) or "匿名设计者"
    defn = validate_definition(definition)
    bootstrap(db)  # 审核流可能指定审核人：确保内置账号已播种
    rule = validate_unlock_rule(db, unlock_rule)
    flow = validate_review_flow(db, review_flow)
    ch = Challenge(title=title, author=author, status="published", current_version=1,
                   unlock_rule_json=json.dumps(rule) if rule else "",
                   created_at=time.time())
    db.add(ch)
    db.flush()  # 取 challenge.id
    _add_version(db, ch.id, 1, defn, flow)
    db.commit()
    return challenge_detail(db, ch.id)


def publish_version(db, challenge_id: int, definition: dict,
                    unlock_rule=_KEEP_RULE, review_flow=_KEEP_RULE) -> dict:
    """发布新版本：定义校验后落库为不可变版本并设为当前版本。

    unlock_rule 缺省（未传）时保留原解锁条件；显式传 {"type":"none"} 可清除。
    review_flow 缺省时沿用当前版本的审核流；显式传 {"stages": [...]} 为新版本
    配置独立的多级审核流，传 {"stages": [], ...} 以外的"空配置"（None 字段）
    即恢复默认单级流。
    """
    ch = _get_challenge(db, challenge_id)
    defn = validate_definition(definition)
    cur = _get_version(db, ch.id, ch.current_version)
    bootstrap(db)  # 审核流可能指定审核人：确保内置账号已播种
    if review_flow is _KEEP_RULE:
        flow = _flow_of(cur)
    else:
        flow = validate_review_flow(db, review_flow)
    _add_version(db, ch.id, ch.current_version + 1, defn, flow)
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


def _flow_of(ver: ChallengeVersion) -> Optional[dict]:
    """版本上的审核流原始配置（None=默认流）。"""
    return json.loads(ver.review_flow_json) if ver.review_flow_json else None


def flow_of(ver: ChallengeVersion) -> dict:
    """版本生效的审核流（未配置时降级为默认单级流）。"""
    return _flow_of(ver) or default_review_flow()


def _public_stage(st: dict, index: int) -> dict:
    return {"stage": index, "name": st["name"], "role": st["role"],
            "reviewers": list(st.get("reviewers") or []),
            "sla_hours": st.get("sla_hours")}


def public_flow(ver: ChallengeVersion) -> dict:
    """对外展示的审核流（含是否自定义、逐级信息与申诉去向）。"""
    raw = _flow_of(ver)
    flow = raw or default_review_flow()
    return {
        "custom": raw is not None,
        "stages": [_public_stage(st, i) for i, st in enumerate(flow["stages"])],
        "appeal_route": flow["appeal_route"],
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
            "review_stages": len(flow_of(v)["stages"]) if v else 1,
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
            "review_flow": public_flow(v),
            "created_at": v.created_at,
        } for v in vers],
        "current": {
            "version": cur.version, "name": cur.name, "brief": cur.brief,
            "hint": cur.hint, "budget_dv": cur.budget_dv, "t_max": cur.t_max,
            "milestones": json.loads(cur.milestones_json),
            "review_flow": public_flow(cur),
        },
        "best": _best_approved(db).get(ch.id),
        "best_current": _best_current_approved(db).get(ch.id),
    }


def version_detail(db, challenge_id: int, version: int) -> dict:
    """指定版本的完整定义（查看旧版本 / 按旧版本游玩）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version)
    return {
        "challenge_id": ch.id, "title": ch.title, "author": ch.author,
        "version": ver.version, "name": ver.name, "brief": ver.brief,
        "hint": ver.hint, "budget_dv": ver.budget_dv, "t_max": ver.t_max,
        "milestones": json.loads(ver.milestones_json),
        "review_flow": public_flow(ver),
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


def _submitted_response(rec: ChallengeSubmission, duplicated: bool,
                        ver: Optional[ChallengeVersion] = None) -> dict:
    flow = flow_of(ver) if ver is not None else None
    out = {
        "saved": True,
        "duplicated": duplicated,
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "stars": rec.stars,
    }
    if flow is not None and rec.review_status == PENDING:
        out["review_stage"] = rec.review_stage or 0
        st = flow["stages"][out["review_stage"]]
        out["review_stage_name"] = st["name"]
        out["review_stage_count"] = len(flow["stages"])
        if st.get("sla_hours"):
            out["sla_deadline"] = (rec.stage_entered_at or rec.created_at) \
                + st["sla_hours"] * 3600
    return out


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
        ver = _get_version(db, run.challenge_id, run.version)
        now = time.time()

        # 3) 落库成绩记录（(challenge_id, submission_id) 唯一约束兜底并发重复）
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
            review_stage=0,  # 进入版本审核流的第 1 级队列
            review_stages_json="[]",
            stage_entered_at=now,
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
                dup_ver = _get_version(db, dup.challenge_id, dup.version)
                return _submitted_response(dup, duplicated=True, ver=dup_ver)
            raise
        _add_event(db, rec, kind=EV_SUBMIT, actor=rec.player,
                   actor_role="player",
                   detail={"submission_id": rec.submission_id,
                           "challenge_id": rec.challenge_id, "version": rec.version,
                           "stars": rec.stars, "fuel_used": rec.fuel_used,
                           "review_flow": public_flow(ver)})
        db.commit()
        return _submitted_response(rec, duplicated=False, ver=ver)


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
    - challenge_submission 再补多级审核列（review_stage / review_stages_json /
      stage_entered_at），challenge_version 补 review_flow_json，
      challenge_appeal 补 route（多级审核流升级，旧库一律默认单级流）；
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
            # 多级审核流相关列（旧库默认值即"默认单级流"语义）
            if cols and "review_stage" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_submission ADD COLUMN review_stage INTEGER")
                conn.commit()
            if cols and "review_stages_json" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_submission ADD COLUMN "
                    "review_stages_json TEXT NOT NULL DEFAULT '[]'")
                conn.commit()
            if cols and "stage_entered_at" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_submission ADD COLUMN stage_entered_at FLOAT")
                conn.commit()
            changed |= _rebuild_scoped_unique(
                conn, "challenge_submission", "challenge_id",
                "uq_chsub_challenge_submission")
        if "challenge_version" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(challenge_version)").fetchall()}
            if cols and "review_flow_json" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_version ADD COLUMN "
                    "review_flow_json TEXT NOT NULL DEFAULT ''")
                conn.commit()
        if "challenge_appeal" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(challenge_appeal)").fetchall()}
            if cols and "route" not in cols:
                conn.execute(
                    "ALTER TABLE challenge_appeal ADD COLUMN "
                    "route VARCHAR(16) NOT NULL DEFAULT 'moderator'")
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


def _backfill_review_stages(db) -> None:
    """旧库 pending 成绩补齐多级审核字段：进入第 1 级，SLA 从提交时起算。

    终态（approved/rejected/revoked）成绩的 review_stage 缺省按 0 解读，
    不强制改写；只有仍在排队的 pending 必须有明确级次才能进正确队列。
    """
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == PENDING,
                      ChallengeSubmission.review_stage.is_(None))
              .all())
    n = 0
    for rec in rows:
        rec.review_stage = 0
        if rec.stage_entered_at is None:
            rec.stage_entered_at = rec.created_at
        if not (rec.review_stages_json or "").strip():
            rec.review_stages_json = "[]"
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
        _backfill_review_stages(db)
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


# ---------- 审核（多级审核流） ----------

def _version_of_submission(db, rec: ChallengeSubmission) -> ChallengeVersion:
    return _get_version(db, rec.challenge_id, rec.version)


def _stage_history(rec: ChallengeSubmission) -> list:
    try:
        h = json.loads(rec.review_stages_json or "[]")
        return h if isinstance(h, list) else []
    except (ValueError, TypeError):
        return []


def _stage_sla_status(rec: ChallengeSubmission, stage: dict,
                      now: float) -> dict:
    """当前级的 SLA 状态：deadline/剩余/是否超时（仅提示，不做自动流转）。"""
    sla = stage.get("sla_hours")
    if not sla or rec.review_status != PENDING:
        return {"sla_hours": sla or None, "sla_deadline": None,
                "sla_overdue": False, "sla_remaining_hours": None}
    entered = rec.stage_entered_at or rec.created_at
    deadline = entered + sla * 3600
    return {"sla_hours": sla, "sla_deadline": deadline,
            "sla_overdue": now > deadline,
            "sla_remaining_hours": round((deadline - now) / 3600, 2)}


def _stage_view(db, rec: ChallengeSubmission, *, now: Optional[float] = None) -> dict:
    """一条 pending 成绩在当前审核级上的完整视图（队列/详情共用）。"""
    now = now if now is not None else time.time()
    ver = _version_of_submission(db, rec)
    flow = flow_of(ver)
    stages = flow["stages"]
    idx = rec.review_stage or 0
    if idx >= len(stages):  # 防御：旧数据/配置错位时停在末级
        idx = len(stages) - 1
    st = stages[idx]
    out = {
        "review_stage": idx,
        "review_stage_name": st["name"],
        "review_stage_index": idx + 1,
        "review_stage_count": len(stages),
        "review_stage_role": st["role"],
        "review_stage_reviewers": list(st.get("reviewers") or []),
        "appeal_route": flow["appeal_route"],
        "stage_history": _stage_history(rec),
    }
    out.update(_stage_sla_status(rec, st, now))
    return out


def _stage_allows(stage: dict, rv: Reviewer) -> bool:
    """该审核人能否处理这个审核级。

    指定审核人名单（reviewers 非空）时只有名单内署名可处理；
    名单为空则该角色全员可处理；复核员角色始终高于初审员。
    """
    named = stage.get("reviewers") or []
    if named:
        return rv.name in named
    if stage["role"] == ROLE_MODERATOR:
        return rv.role == ROLE_MODERATOR
    return rv.role in (ROLE_REVIEWER, ROLE_MODERATOR)


def _append_stage(rec, *, stage: dict, idx: int, action: str, actor: str,
                  note: str, now: float) -> None:
    hist = _stage_history(rec)
    hist.append({"stage": idx, "name": stage["name"], "action": action,
                 "actor": actor, "note": note, "created_at": now})
    rec.review_stages_json = json.dumps(hist, ensure_ascii=False)


def review(db, *, record_id: int, action: str, note: str = "",
           reviewer_token: Optional[str] = None,
           stage: Optional[int] = None) -> dict:
    """多级审核状态机：在版本配置的当前审核级上 approve/reject。

    - 提交后进入第 1 级；某级 approve 进入下一级，末级 approve 才 approved
      （进入排行榜/回放/解锁）；任意一级 reject 即 rejected。
    - 审核级可指定审核人（名单外 403）、角色（复核级初审员 403）与时限（超时标记）。
    - appeal_route=stage 的版本，申诉成绩回到驳回级由该级重审：该级 approve
      视为推翻原判并继续向后流转（可能还有后续级），reject 视为维持原判。
    - appeal_route=moderator（默认）时申诉中成绩只能走复核裁决接口。
    - 多级流程须显式带 stage（当前级）防误处理；级不匹配 → StageConflict(409)。
    - 幂等：重复同一终审动作返回当前状态（duplicated=True）；
      已终审的成绩再作相反动作 → ReviewConflict(409)。
    """
    if action not in _REVIEW_ACTIONS:
        raise ValidationError(f"未知审核动作: {action}")
    target = _REVIEW_ACTIONS[action]
    # 锁外先做轻量凭证校验（无 token 降级内置初审员）；级级授权在锁内判定
    rv = authenticate(db, reviewer_token, required_role=ROLE_REVIEWER)
    with _LOCK:
        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
        ver = _version_of_submission(db, rec)
        flow = flow_of(ver)
        stages = flow["stages"]
        cur_idx = rec.review_stage or 0
        open_appeal = _open_appeal(db, rec.id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        now = time.time()

        def resp(duplicated: bool) -> dict:
            return _review_response(
                rec, target if rec.review_status == target else rec.review_status,
                duplicated=duplicated, actor=rv.name, ver=ver,
                effects=_effects(db, before, rec, replayable_before=replay_before))

        # 已终审：幂等 / 冲突（与历史行为一致）
        if rec.review_status in (APPROVED, REJECTED, REVOKED):
            if rec.review_status == target and open_appeal is None:
                return resp(duplicated=True)
            raise ReviewConflict(
                f"成绩已审核（{rec.review_status}），不能重复审核")

        # pending：定位审核级
        if cur_idx >= len(stages):
            raise StageConflict("审核级配置与成绩状态不一致")
        if stage is not None and stage != cur_idx:
            raise StageConflict(
                f"成绩当前在第 {cur_idx + 1} 级「{stages[cur_idx]['name']}」"
                f"，不能按第 {stage + 1} 级处理")
        if len(stages) > 1 and stage is None:
            raise StageConflict(
                f"该版本为多级审核（{len(stages)} 级），请指定要处理的审核级")
        st = stages[cur_idx]
        if not _stage_allows(st, rv):
            named = st.get("reviewers") or []
            if named:
                raise ForbiddenReviewer(
                    f"第 {cur_idx + 1} 级「{st['name']}」仅限指定审核人处理："
                    + "、".join(named))
            raise ForbiddenReviewer(
                f"第 {cur_idx + 1} 级「{st['name']}」需要复核员权限")
        note = (note or "").strip()[:200]

        # 申诉重审中的成绩：按版本 appeal_route 决定处理方式
        if open_appeal is not None:
            if open_appeal.route != APPEAL_ROUTE_STAGE:
                raise ReviewConflict(
                    "成绩正在复核员申诉裁决中，请通过复核裁决（uphold/overturn）处理")
            appeal = open_appeal
            if action == "reject":
                # 该级重审驳回 = 维持原判
                appeal.status = APPEAL_UPHOLD
                appeal.decision_note = note
                appeal.decided_by = rv.name
                appeal.decided_at = now
                rec.review_status = appeal.from_status
                rec.review_note = note
                rec.reviewed_by = rv.name
                rec.reviewed_at = now
                _add_event(db, rec, kind=EV_APPEAL_DECISION[APPEAL_UPHOLD],
                           actor=rv.name, actor_role=st["role"],
                           detail={"round": appeal.round,
                                   "from_status": appeal.from_status,
                                   "stage": cur_idx, "stage_name": st["name"],
                                   "note": note}, appeal=appeal)
                db.commit()
                return _appeal_stage_response(
                    rec, ver, appeal, APPEAL_UPHOLD, duplicated=False, actor=rv.name,
                    effects=_effects(db, before, rec, replayable_before=replay_before))
            # 该级重审通过 = 推翻驳回，继续向后流转（可能还有后续级）
            appeal.status = APPEAL_OVERTURN
            appeal.decision_note = note
            appeal.decided_by = rv.name
            appeal.decided_at = now
            _add_event(db, rec, kind=EV_APPEAL_DECISION[APPEAL_OVERTURN],
                       actor=rv.name, actor_role=st["role"],
                       detail={"round": appeal.round,
                               "from_status": appeal.from_status,
                               "stage": cur_idx, "stage_name": st["name"],
                               "note": note}, appeal=appeal)
            _advance_approved(db, rec, st, cur_idx, rv, note, now, ver,
                              include_stage_event=False)
            db.commit()
            return _appeal_stage_response(
                rec, ver, appeal, APPEAL_OVERTURN, duplicated=False, actor=rv.name,
                effects=_effects(db, before, rec, replayable_before=replay_before))

        # 常规审核：驳回即终态
        if action == "reject":
            rec.review_status = REJECTED
            rec.review_note = note
            rec.reviewed_by = rv.name
            rec.reviewed_at = now
            _append_stage(rec, stage=st, idx=cur_idx, action="reject",
                          actor=rv.name, note=note, now=now)
            _add_event(db, rec, kind=EV_REVIEW[REJECTED], actor=rv.name,
                       actor_role=st["role"],
                       detail={"stage": cur_idx, "stage_name": st["name"],
                               "note": note})
            db.commit()
            return resp(duplicated=False)

        # 常规通过：末级 → approved，否则进入下一级队列
        _advance_approved(db, rec, st, cur_idx, rv, note, now, ver)
        db.commit()
        return resp(duplicated=False)


def _advance_approved(db, rec, stage, idx, rv, note, now, ver,
                      *, include_stage_event: bool = True) -> None:
    """某级通过后的统一推进：记录该级结果，末级上榜、否则进入下一级队列。"""
    _append_stage(rec, stage=stage, idx=idx, action="approve",
                  actor=rv.name, note=note, now=now)
    if include_stage_event:
        _add_event(db, rec, kind=EV_REVIEW[APPROVED], actor=rv.name,
                   actor_role=stage["role"],
                   detail={"stage": idx, "stage_name": stage["name"],
                           "note": note, "final": idx == len(flow_of(ver)["stages"]) - 1})
    stages = flow_of(ver)["stages"]
    if idx + 1 >= len(stages):
        rec.review_status = APPROVED
        rec.review_note = note
        rec.reviewed_by = rv.name
        rec.reviewed_at = now
    else:
        rec.review_stage = idx + 1
        rec.stage_entered_at = now


def _appeal_stage_response(rec, ver, appeal, decision, *, duplicated: bool,
                           actor: str, effects: dict) -> dict:
    out = _review_response(rec, rec.review_status, duplicated=duplicated,
                           actor=actor, ver=ver, effects=effects)
    out["appeal_id"] = appeal.id
    out["appeal_round"] = appeal.round
    out["appeal_decision"] = decision
    return out


def _review_response(rec, status, *, duplicated: bool, actor: str,
                     effects: Optional[dict] = None,
                     ver: Optional[ChallengeVersion] = None) -> dict:
    out = {"record_id": rec.id, "challenge_id": rec.challenge_id,
           "version": rec.version, "review_status": status,
           "reviewed_by": actor, "duplicated": duplicated,
           "review_stage": rec.review_stage,
           "stage_history": _stage_history(rec),
           "effects": effects}
    if ver is not None and status == PENDING:
        flow = flow_of(ver)
        idx = rec.review_stage or 0
        if idx < len(flow["stages"]):
            st = flow["stages"][idx]
            out["review_stage"] = idx
            out["review_stage_name"] = st["name"]
            out["review_stage_index"] = idx + 1
            out["review_stage_count"] = len(flow["stages"])
    return out


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
        ver = _version_of_submission(db, rec)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == REVOKED:
            return _moderation_response(rec, EV_REVOKE, duplicated=True,
                                        actor=rv.name, ver=ver,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != APPROVED:
            raise ReviewConflict(
                f"只有已通过的成绩可以撤销（当前 {rec.review_status}）")
        if _open_appeal(db, rec.id) is not None:
            raise ReviewConflict("成绩正在申诉复核中，不能撤销")
        _reject_terminal(db, rec, status=REVOKED, note=note, reviewer=rv,
                         kind=EV_REVOKE, stage_idx=len(flow_of(ver)["stages"]) - 1)
        db.commit()
        return _moderation_response(rec, EV_REVOKE, duplicated=False,
                                    actor=rv.name, ver=ver,
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
        ver = _version_of_submission(db, rec)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == APPROVED:
            return _moderation_response(rec, EV_RESTORE, duplicated=True,
                                        actor=rv.name, ver=ver,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != REVOKED:
            raise ReviewConflict(
                f"只有已撤销的成绩可以恢复（当前 {rec.review_status}）")
        now = time.time()
        rec.review_status = APPROVED
        rec.review_note = (note or "").strip()[:200]
        rec.reviewed_by = rv.name
        rec.reviewed_at = now
        # 恢复为已上榜：审核级停在末级（终态）
        rec.review_stage = len(flow_of(ver)["stages"]) - 1
        _add_event(db, rec, kind=EV_RESTORE, actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"note": rec.review_note})
        db.commit()
        return _moderation_response(rec, EV_RESTORE, duplicated=False,
                                    actor=rv.name, ver=ver,
                                    effects=_effects(db, before, rec,
                                                     replayable_before=replay_before))


def _moderation_response(rec, action_kind, *, duplicated: bool, actor: str,
                         effects: dict, ver=None) -> dict:
    return {"record_id": rec.id, "challenge_id": rec.challenge_id,
            "version": rec.version, "action": action_kind,
            "review_status": rec.review_status,
            "reviewed_by": actor, "duplicated": duplicated,
            "review_stage": rec.review_stage,
            "stage_history": _stage_history(rec),
            "effects": effects}


def _reject_terminal(db, rec, *, status: str, note: str, reviewer: Reviewer,
                     kind: str, stage_idx: Optional[int] = None) -> None:
    now = time.time()
    rec.review_status = status
    rec.review_note = (note or "").strip()[:200]
    rec.reviewed_by = reviewer.name
    rec.reviewed_at = now
    if stage_idx is not None:
        rec.review_stage = stage_idx
    _add_event(db, rec, kind=kind, actor=reviewer.name,
               actor_role=ROLE_MODERATOR, detail={"note": rec.review_note})


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
    受理后成绩回到 pending（标注申诉轮次）进入复核队列，等待复核员裁决。
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
            raise AppealConflict("该成绩已有进行中的申诉，请等待复核裁决")
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == rec.id)
                    .count())
        if rounds >= MAX_APPEAL_ROUNDS:
            raise AppealConflict(f"每条成绩最多申诉 {MAX_APPEAL_ROUNDS} 次")
        ver = _version_of_submission(db, rec)
        flow = flow_of(ver)
        # revoked（上榜后撤销）始终由复核员受理；rejected 的去向按版本配置：
        # appeal_route=stage 时回到驳回时的审核级，由该级指定审核人重审。
        if rec.review_status == REVOKED or flow["appeal_route"] != APPEAL_ROUTE_STAGE:
            route = APPEAL_ROUTE_MODERATOR
            reenter_stage = None
        else:
            route = APPEAL_ROUTE_STAGE
            hist = _stage_history(rec)
            rejected = [h for h in hist if h.get("action") == "reject"]
            reenter_stage = rejected[-1]["stage"] if rejected else (rec.review_stage or 0)
        now = time.time()
        appeal = ChallengeAppeal(
            appeal_uid=appeal_id or uuid.uuid4().hex,
            submission_id=rec.id, round=rounds + 1,
            from_status=rec.review_status, player=player,
            reason=reason, status=APPEAL_PENDING, route=route,
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
        # 成绩回到待审队列：route=stage 回驳回级（SLA 重新计时），
        # route=moderator 保持当前级但只接受复核裁决；排行榜/回放/解锁随状态回滚。
        rec.review_status = PENDING
        if route == APPEAL_ROUTE_STAGE:
            rec.review_stage = reenter_stage
            rec.stage_entered_at = now
        _add_event(db, rec, kind=EV_APPEAL, actor=player, actor_role="player",
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "route": route,
                           "stage": rec.review_stage,
                           "reason": reason}, appeal=appeal)
        db.commit()
        return _appeal_response(appeal, duplicated=False)


def decide_appeal(db, *, appeal_id_key: int, decision: str, note: str = "",
                  moderator_token: Optional[str] = None) -> dict:
    """复核员裁决申诉：uphold=维持原判（成绩回到 from_status）；
    overturn=推翻原判（改判通过 approved，重新上榜/回放/解锁）。

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
        ver = _version_of_submission(db, rec)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        # route=stage 的申诉由驳回级审核人在审核接口裁决，复核员不直接处理
        if appeal.status == APPEAL_PENDING and appeal.route == APPEAL_ROUTE_STAGE:
            raise ReviewConflict(
                "该申诉配置为回到驳回审核级重审，请由该级审核人在审核队列处理")
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
        # 复核员推翻直接上榜 → 停在末级；维持 → 回到原终态级
        last_stage = len(flow_of(ver)["stages"]) - 1
        rec.review_stage = last_stage if decision == APPEAL_OVERTURN else \
            (rec.review_stage or 0)
        _add_event(db, rec, kind=EV_APPEAL_DECISION[decision], actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "route": appeal.route,
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
            "route": ap.route,
            "review_status": PENDING}


def _appeal_decision_response(ap, rec, decision, *, duplicated: bool,
                              actor: str, effects: dict) -> dict:
    return {"appeal_id": ap.id, "record_id": rec.id,
            "challenge_id": rec.challenge_id, "version": rec.version,
            "decision": decision, "status": decision,
            "review_status": rec.review_status,
            "reviewed_by": actor, "round": ap.round,
            "duplicated": duplicated, "effects": effects}


def list_appeals(db, *, status: str = APPEAL_PENDING,
                 route: Optional[str] = None,
                 limit: int = 50) -> List[dict]:
    """申诉队列：默认待复核（moderator 工作流）。

    route=moderator 只回复核员裁决队列；route=stage 只回回到审核级重审的申诉。
    缺省（待裁决视图）只含 moderator 路由，避免复核员看到不归其处理的申诉；
    显式 route=all 可查看全部。
    """
    q = db.query(ChallengeAppeal)
    if status in (APPEAL_PENDING, APPEAL_UPHOLD, APPEAL_OVERTURN):
        q = q.filter(ChallengeAppeal.status == status)
    if route is None:
        # 缺省只回复核员受理的申诉（route=stage 的在审核队列处理）
        q = q.filter(ChallengeAppeal.route == APPEAL_ROUTE_MODERATOR)
    elif route in (APPEAL_ROUTE_MODERATOR, APPEAL_ROUTE_STAGE):
        q = q.filter(ChallengeAppeal.route == route)
    rows = (q.order_by(ChallengeAppeal.id.desc())
             .limit(max(1, min(200, limit))).all())
    sub_ids = {a.submission_id for a in rows}
    subs = {s.id: s for s in
            (db.query(ChallengeSubmission)
               .filter(ChallengeSubmission.id.in_(sub_ids)).all())} if sub_ids else {}
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    out = []
    for a in rows:
        s = subs.get(a.submission_id)
        item = {
            "appeal_id": a.id, "record_id": a.submission_id,
            "challenge_id": s.challenge_id if s else None,
            "challenge_title": titles.get(s.challenge_id, f"#{s.challenge_id}")
                               if s else "",
            "version": s.version if s else None,
            "player": a.player, "round": a.round,
            "from_status": a.from_status, "status": a.status,
            "route": a.route,
            "reason": a.reason, "decision_note": a.decision_note,
            "decided_by": a.decided_by,
            "stars": s.stars if s else None,
            "fuel_used": s.fuel_used if s else None,
            "elapsed_days": s.elapsed_days if s else None,
            "created_at": a.created_at, "decided_at": a.decided_at,
        }
        if s is not None and a.status == APPEAL_PENDING:
            item["review_stage"] = s.review_stage or 0
            ver = _get_version(db, s.challenge_id, s.version)
            flow = flow_of(ver)
            idx = item["review_stage"]
            if idx < len(flow["stages"]):
                st = flow["stages"][idx]
                item["review_stage_name"] = st["name"]
                item["review_stage_reviewers"] = list(st.get("reviewers") or [])
                item["review_stage_role"] = st["role"]
        out.append(item)
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
        flow = flow_of(ver) if ver is not None else default_review_flow()
        stages = flow["stages"]
        cur_idx = r.review_stage or 0
        st = stages[cur_idx] if cur_idx < len(stages) else stages[-1]
        out.append({
            "record_id": r.id, "challenge_id": r.challenge_id,
            "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
            "version": r.version, "stars": r.stars,
            "fuel_used": r.fuel_used, "elapsed_days": r.elapsed_days,
            "review_status": r.review_status, "review_note": r.review_note,
            "reviewed_by": r.reviewed_by,
            "review_stage": cur_idx,
            "review_stage_name": st["name"],
            "review_stage_index": min(cur_idx + 1, len(stages)),
            "review_stage_count": len(stages),
            "stage_history": _stage_history(r),
            "appeal_route": flow["appeal_route"],
            "appeal_rounds": rounds,
            "appeal_open": ap.id if ap else None,
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
    ver = _get_version(db, rec.challenge_id, rec.version)
    flow = flow_of(ver)
    cur_idx = rec.review_stage or 0
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
        "review_status": rec.review_status, "review_note": rec.review_note,
        "reviewed_by": rec.reviewed_by,
        "review_flow": public_flow(ver),
        "review_stage": cur_idx,
        "review_stage_name": (flow["stages"][cur_idx]["name"]
                              if cur_idx < len(flow["stages"]) else None),
        "review_stage_index": min(cur_idx + 1, len(flow["stages"])),
        "review_stage_count": len(flow["stages"]),
        "stage_history": _stage_history(rec),
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
            "route": a.route,
            "reason": a.reason, "status": a.status,
            "decision_note": a.decision_note, "decided_by": a.decided_by,
            "created_at": a.created_at, "decided_at": a.decided_at,
        } for a in appeals],
    }


def list_submissions(db, *, challenge_id: Optional[int] = None,
                     status: str = "pending", limit: int = 50,
                     stage: Optional[int] = None,
                     reviewer_token: Optional[str] = None) -> List[dict]:
    """成绩提交列表：默认待审核队列（多级审核按当前级展示，附申诉/SLA 标记）。

    - stage= 指定只看某个审核级（按各版本自己的级编号）；
    - 提供 reviewer_token 时为每条待审成绩计算 can_review（名单/角色是否匹配）；
      申诉中且 route=moderator 的成绩对审核队列标记 can_review=false。
    """
    q = db.query(ChallengeSubmission)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    if status in ("pending", "approved", "rejected", "revoked"):
        q = q.filter(ChallengeSubmission.review_status == status)
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(200, limit)))
             .all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    versions = {(v.challenge_id, v.version): v
                for v in db.query(ChallengeVersion).all()}
    rv = None
    if reviewer_token:
        try:
            rv = authenticate(db, reviewer_token, required_role=ROLE_REVIEWER)
        except (UnauthorizedReviewer, ForbiddenReviewer):
            rv = None
    now = time.time()
    result = []
    for r in rows:
        ap = _open_appeal(db, r.id)
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == r.id).count())
        ver = versions.get((r.challenge_id, r.version))
        flow = flow_of(ver) if ver is not None else default_review_flow()
        stages = flow["stages"]
        cur_idx = r.review_stage or 0
        if stage is not None and cur_idx != stage:
            continue
        st = stages[cur_idx] if cur_idx < len(stages) else stages[-1]
        item = {
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
            "review_stage": cur_idx,
            "review_stage_name": st["name"],
            "review_stage_index": min(cur_idx + 1, len(stages)),
            "review_stage_count": len(stages),
            "review_stage_role": st["role"],
            "review_stage_reviewers": list(st.get("reviewers") or []),
            "appeal_route": flow["appeal_route"],
            "stage_history": _stage_history(r),
            "appeal_open": ap.id if ap else None,
            "appeal_round": ap.round if ap else None,
            "appeal_reason": ap.reason if ap else None,
            "appeal_route_open": ap.route if ap else None,
            "appeal_rounds_total": rounds,
            "created_at": r.created_at,
        }
        item.update(_stage_sla_status(r, st, now))
        if r.review_status == PENDING:
            if ap is not None and ap.route == APPEAL_ROUTE_MODERATOR:
                item["can_review"] = False
                item["queue_kind"] = "moderator_appeal"
            elif rv is not None:
                item["can_review"] = _stage_allows(st, rv)
                item["queue_kind"] = ("stage_appeal" if ap is not None
                                      else "stage")
            else:
                item["can_review"] = None  # 未提供凭证：不判定，仅展示
                item["queue_kind"] = "stage_appeal" if ap is not None else "stage"
        result.append(item)
    return result


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
    ver = _get_version(db, rec.challenge_id, rec.version)
    flow = flow_of(ver)
    cur_idx = rec.review_stage or 0
    st_view = _stage_view(db, rec) if rec.review_status == PENDING else None
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
        "review_flow": public_flow(ver),
        "review_stage": cur_idx,
        "review_stage_name": (flow["stages"][cur_idx]["name"]
                              if cur_idx < len(flow["stages"]) else None),
        "review_stage_index": min(cur_idx + 1, len(flow["stages"])),
        "review_stage_count": len(flow["stages"]),
        "stage_history": _stage_history(rec),
        "appeal_rounds": rounds,
        "appeal_open": open_ap.id if open_ap else None,
        "appeal_route_open": open_ap.route if open_ap else None,
        "appeal_available": (
            rec.review_status in (REJECTED, REVOKED)
            and open_ap is None and rounds < MAX_APPEAL_ROUNDS),
        "replayable": rec.review_status == "approved",
        "created_at": rec.created_at,
    }
    if st_view is not None:
        detail.update({k: st_view[k] for k in (
            "sla_hours", "sla_deadline", "sla_overdue", "sla_remaining_hours")})
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
