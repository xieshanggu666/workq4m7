"""社区挑战多级审核测试：按版本配置审核链（审核人 + 时限）/ 逐级流转 /
申诉进入正确队列 / 撤销恢复与排行榜、回放、解锁的联动。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SLINGSHOT_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="slingshot_pipe_test_"), "test.db")

import pytest
from fastapi import HTTPException

from app.core.database import Base, SessionLocal, engine
from app.models import (Challenge, ChallengeAppeal, ChallengeReviewEvent,
                        ChallengeRun, ChallengeSubmission, ChallengeVersion,
                        LevelScore, Reviewer, RunRecord, ScoreRecord)
from app.api import challenges as ch_api
from app.services import challenges as ch_svc

MOD_TOKEN = "local-moderator"
REV_TOKEN = "local-reviewer"


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(bind=engine)
    ch_svc.reset_bootstrap_for_tests()
    db = SessionLocal()
    try:
        for t in (ChallengeAppeal, ChallengeReviewEvent, ChallengeSubmission,
                  ChallengeRun, ChallengeVersion, Challenge,
                  ScoreRecord, RunRecord, LevelScore):
            db.query(t).delete()
        db.query(Reviewer).delete()
        db.commit()
        ch_svc.bootstrap(db)
        yield
    finally:
        db.close()


# ---------- 工具 ----------

def _def(**over):
    d = {
        "name": "首版航线", "brief": "飞掠火星即可", "hint": "切向点火",
        "budget_dv": 0.004, "t_max": 900,
        "milestones": [{"kind": "proximity", "planet_id": "mars",
                        "dist": 0.18, "name": "飞掠火星"}],
    }
    d.update(over)
    return d


def _stage(name="初审", role="reviewer", reviewers=None, hours=None):
    s = {"name": name, "role": role}
    if reviewers is not None:
        s["reviewers"] = reviewers
    if hours is not None:
        s["time_limit_hours"] = hours
    return s


def _pipeline2(*, l1_reviewers=None, l1_hours=None, l2_hours=None):
    """标准两级链：初审员级 → 复核员级（终审）。"""
    return [
        _stage("社区初审", "reviewer", l1_reviewers, l1_hours),
        _stage("官方终审", "moderator", None, l2_hours),
    ]


def _create(title="两级挑战", pipeline=_pipeline2(), **over):
    payload = {"title": title, "author": "测试员", **_def(),
               "review_pipeline": pipeline}
    payload.update(over)
    return ch_api.create_challenge(ch_api.CreateChallengeIn(**payload))


def _run(cid, actions=None):
    actions = actions or [{"type": "burn", "angle": 90.0, "dv": 0.0017}]
    return ch_api.run(cid, ch_api.SimIn(
        actions=[ch_api.Action(**a) for a in actions]))


def _submit(cid, player="飞行员甲", sub_id=None):
    import uuid
    r = _run(cid)
    return ch_api.submit(cid, ch_api.SubmitIn(
        run_id=r["run_id"], submission_id=sub_id or uuid.uuid4().hex,
        player=player)), r


def _review(record_id, action, token=None, level=None):
    return ch_api.review_submission(
        record_id, ch_api.ReviewIn(action=action, note=""), token, level)


def _appeal(record_id, reason="请求重审", ap_id=None, player="飞行员甲"):
    import uuid
    return ch_api.appeal_submission(record_id, ch_api.AppealIn(
        player=player, reason=reason,
        appeal_id=ap_id or uuid.uuid4().hex))


def _decide(appeal_id, decision, token=MOD_TOKEN):
    return ch_api.decide_appeal(appeal_id,
                                ch_api.AppealDecisionIn(decision=decision),
                                token)


def _revoke(record_id, token=MOD_TOKEN):
    return ch_api.revoke_submission(record_id, ch_api.ModerationIn(note="撤销"),
                                    token)


def _restore(record_id, token=MOD_TOKEN):
    return ch_api.restore_submission(record_id, ch_api.ModerationIn(note="恢复"),
                                     token)


def _register(name, token, role="reviewer"):
    return ch_api.create_reviewer(ch_api.ReviewerIn(
        name=name, token=token, role=role), MOD_TOKEN)


def _row(record_id):
    db = SessionLocal()
    try:
        return (db.query(ChallengeSubmission)
                .filter(ChallengeSubmission.id == record_id).one())
    finally:
        db.close()


# ---------- 审核链配置（按版本、不可变） ----------

def test_default_pipeline_is_legacy_single_stage():
    d = ch_api.create_challenge(ch_api.CreateChallengeIn(
        title="默认挑战", author="测试员", **_def()))  # 不带审核链
    assert d["current"]["review_pipeline"]["levels"] == 1
    assert d["current"]["review_pipeline"]["legacy"] is True
    v = ch_api.version_detail(d["id"], 1)
    assert v["review_pipeline"]["stages"][0]["role"] == "reviewer"
    db = SessionLocal()
    try:
        ver = db.query(ChallengeVersion).one()
        assert ver.review_pipeline_json == ""  # 默认链路不落 JSON
    finally:
        db.close()


def test_create_with_multi_stage_pipeline_persists_per_version():
    d = _create()
    summary = d["current"]["review_pipeline"]
    assert summary["levels"] == 2 and summary["legacy"] is False
    assert [s["name"] for s in summary["stages"]] == ["社区初审", "官方终审"]
    assert [s["role"] for s in summary["stages"]] == ["reviewer", "moderator"]
    v = ch_api.version_detail(d["id"], 1)
    assert v["review_pipeline"]["stages"][1]["time_limit_hours"] is None
    # 列表卡片也携带审核链摘要
    card = next(c for c in ch_api.list_challenges()["challenges"] if c["id"] == d["id"])
    assert card["review_pipeline"]["levels"] == 2


def test_publish_version_inherits_then_overrides_pipeline():
    d = _create()
    # 发新版不带审核链字段 → 沿用上一版配置
    v2 = ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(name="v2")))
    assert v2["current"]["review_pipeline"]["levels"] == 2
    assert v2["versions"][1]["review_pipeline"]["levels"] == 2
    # 显式传 null → 回到旧版默认单级；旧 v1/v2 配置不可变
    v3 = ch_api.publish_version(d["id"], ch_api.VersionIn(
        **_def(name="v3"), review_pipeline=None))
    assert v3["current"]["review_pipeline"]["legacy"] is True
    assert ch_api.version_detail(d["id"], 1)["review_pipeline"]["levels"] == 2
    assert ch_api.version_detail(d["id"], 2)["review_pipeline"]["levels"] == 2
    assert ch_api.version_detail(d["id"], 3)["review_pipeline"]["legacy"] is True


def test_pipeline_validation_errors():
    bad_pipelines = [
        [],                                          # 至少 1 级
        [_stage() for _ in range(6)],                # 超过 5 级
        [_stage(role="wizard")],                     # 未知角色
        [_stage(reviewers=["不存在的审核员"])],       # 指定审核人不存在
        # 复核级指定初审员
        [_stage("终审", "moderator", ["本机初审员"])],
    ]
    for pipe in bad_pipelines:
        with pytest.raises(HTTPException) as e:
            _create(title="非法链", pipeline=pipe)
        assert e.value.status_code == 400, pipe
    # 级别名重复
    with pytest.raises(HTTPException) as e:
        _create(pipeline=[_stage("同名"), _stage("同名", "moderator")])
    assert e.value.status_code == 400
    # 时限越界
    with pytest.raises(HTTPException) as e:
        _create(pipeline=[_stage(hours=0.01)])
    assert e.value.status_code == 400
    # 指定存在的初审员 → 通过
    d = _create(title="白名单链",
                pipeline=[_stage("指定初审", "reviewer", ["本机初审员"])])
    assert d["current"]["review_pipeline"]["stages"][0]["reviewers"] == ["本机初审员"]


# ---------- 逐级流转 ----------

def test_submission_starts_at_level_one_queue():
    d = _create()
    s, _ = _submit(d["id"])
    assert s["review_status"] == "pending" and s["review_level"] == 1
    q1 = ch_api.review_queue(level=1)["submissions"]
    q2 = ch_api.review_queue(level=2)["submissions"]
    row = next(x for x in q1 if x["record_id"] == s["record_id"])
    assert row["review_level"] == 1 and row["total_levels"] == 2
    assert row["stage"]["name"] == "社区初审" and row["stage"]["final_level"] is False
    assert all(x["record_id"] != s["record_id"] for x in q2)


def test_advance_through_stages_then_final_approve():
    a = _create(title="前置")
    b = _create(title="后续",
                unlock_rule={"type": "challenge", "challenge_id": a["id"]})
    # b 默认解锁无关；改用 a 的通过联动另一个挑战
    dep = ch_api.create_challenge(ch_api.CreateChallengeIn(
        title="依赖挑战", author="测试员", **_def(),
        unlock_rule={"type": "challenge", "challenge_id": a["id"]},
        review_pipeline=None))

    s, _ = _submit(a["id"])
    # 第 1 级通过：进入第 2 级，仍不上榜/不回放/不解锁
    out1 = _review(s["record_id"], "approve", token=REV_TOKEN, level=1)
    assert out1["advanced"] is True
    assert out1["review_status"] == "pending" and out1["review_level"] == 2
    assert out1["stage"]["name"] == "官方终审" and out1["stage"]["final_level"] is True
    assert out1["effects"]["leaderboard"]["on_board"] is False
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    assert next(c for c in ch_api.list_challenges()["challenges"]
                if c["id"] == dep["id"])["unlocked"] is False
    # 初审员不能处理复核级
    with pytest.raises(HTTPException) as e:
        _review(s["record_id"], "approve", token=REV_TOKEN)
    assert e.value.status_code == 403
    # 终审级通过：上榜 / 回放 / 解锁联动
    out2 = _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    assert out2["advanced"] is False and out2["review_status"] == "approved"
    assert out2["effects"]["leaderboard"]["on_board"] is True
    entries = ch_api.get_leaderboard(a["id"])["entries"]
    assert [e["record_id"] for e in entries] == [s["record_id"]]
    assert ch_api.get_submission(s["record_id"])["replayable"] is True
    assert next(c for c in ch_api.list_challenges()["challenges"]
                if c["id"] == dep["id"])["unlocked"] is True
    # 待审队列清空
    assert ch_api.review_queue()["submissions"] == []


def test_reject_at_early_level_never_reaches_board():
    d = _create()
    s, _ = _submit(d["id"])
    out = _review(s["record_id"], "reject", token=REV_TOKEN, level=1)
    assert out["review_status"] == "rejected" and out["advanced"] is False
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    assert ch_api.review_queue(level=1)["submissions"] == []
    assert ch_api.review_queue(level=2)["submissions"] == []


def test_stage_whitelist_authorizes_only_named_reviewers():
    # 第 1 级仅允许指定的新初审员；内置初审员与复核令牌之外的人不可处理
    _register("专家甲", "t-expert")
    d = _create(pipeline=[
        _stage("专家初审", "reviewer", ["专家甲"]),
        _stage("官方终审", "moderator"),
    ])
    s, _ = _submit(d["id"])
    # 缺省内置初审员不在名单 → 403
    with pytest.raises(HTTPException) as e1:
        _review(s["record_id"], "approve")
    assert e1.value.status_code == 403
    # 内置初审员令牌同样 403
    with pytest.raises(HTTPException) as e2:
        _review(s["record_id"], "approve", token=REV_TOKEN)
    assert e2.value.status_code == 403
    # 指定审核人放行（推进到第 2 级）
    out = _review(s["record_id"], "approve", token="t-expert")
    assert out["review_level"] == 2


def test_moderator_can_handle_any_stage_including_override():
    d = _create(pipeline=[_stage("受限初审", "reviewer", ["本机初审员"])])
    s, _ = _submit(d["id"])
    # 复核员可处理初审级（超时/争议兜底）
    out = _review(s["record_id"], "approve", token=MOD_TOKEN)
    assert out["review_status"] == "approved"


def test_expect_level_mismatch_conflicts():
    d = _create()
    s, _ = _submit(d["id"])
    with pytest.raises(HTTPException) as e:
        _review(s["record_id"], "approve", token=REV_TOKEN, level=2)
    assert e.value.status_code == 409


# ---------- 时限（SLA） ----------

def test_stage_sla_due_at_and_overdue_flag():
    d = _create(pipeline=_pipeline2(l1_hours=24, l2_hours=12))
    s, _ = _submit(d["id"])
    row = next(x for x in ch_api.review_queue(level=1)["submissions"]
               if x["record_id"] == s["record_id"])
    assert row["stage"]["time_limit_hours"] == 24
    assert row["stage"]["due_at"] > time.time()
    assert row["stage"]["overdue"] is False
    assert ch_api.review_queue(level=1, overdue=True)["submissions"] == []
    # 把进入第 1 级的时间拨到 25 小时前 → 超时队列可见
    rec = _row(s["record_id"])
    db = SessionLocal()
    try:
        r = db.query(ChallengeSubmission).filter(
            ChallengeSubmission.id == s["record_id"]).one()
        r.review_stage_entered_at = time.time() - 25 * 3600
        db.commit()
    finally:
        db.close()
    overdue = ch_api.review_queue(level=1, overdue=True)["submissions"]
    assert [x["record_id"] for x in overdue] == [s["record_id"]]
    row = next(x for x in ch_api.review_queue(level=1)["submissions"]
               if x["record_id"] == s["record_id"])
    assert row["stage"]["overdue"] is True
    # 超时不自动改判；复核员兜底处理仍可通过
    out = _review(s["record_id"], "approve", token=MOD_TOKEN)
    assert out["review_level"] == 2
    l2 = next(x for x in ch_api.review_queue(level=2)["submissions"]
              if x["record_id"] == s["record_id"])
    assert l2["stage"]["time_limit_hours"] == 12


# ---------- 申诉进入正确队列 ----------

def test_appeal_after_level_rejection_returns_to_that_level():
    d = _create()
    s, _ = _submit(d["id"])
    _review(s["record_id"], "reject", token=REV_TOKEN, level=1)
    ap = _appeal(s["record_id"], reason="第 1 级误判")
    assert ap["target_level"] == 1 and ap["review_level"] == 1
    # 进入第 1 级审核队列（带路由标记），而不是复核员旧队列
    q1 = ch_api.review_queue(level=1)["submissions"]
    row = next(x for x in q1 if x["record_id"] == s["record_id"])
    assert row["appeal_open"] is not None and row["appeal_target_level"] == 1
    assert row["appeal_routed"] is True and row["appeal_legacy"] is False
    assert ch_api.appeals()["appeals"] == []  # 复核员旧队列看不到
    # 旧复核裁决接口拒绝处理已路由的申诉
    with pytest.raises(HTTPException) as e:
        _decide(ap["appeal_id"], "overturn")
    assert e.value.status_code == 409
    # 第 1 级维持（在审核接口驳回）→ 回到 rejected
    out = _review(s["record_id"], "reject", token=REV_TOKEN, level=1)
    assert out["review_status"] == "rejected"
    assert out["appeal_round"] == 1
    # 第 2 轮申诉：第 1 级推翻（通过）→ 继续流转到第 2 级
    ap2 = _appeal(s["record_id"], reason="补充证据", ap_id="ap-round-2")
    assert ap2["round"] == 2 and ap2["target_level"] == 1
    out1 = _review(s["record_id"], "approve", token=REV_TOKEN, level=1)
    assert out1["advanced"] is True and out1["review_level"] == 2
    assert ch_api.get_leaderboard(d["id"])["entries"] == []  # 仍未上榜
    # 第 2 级（终审）推翻通过 → 上榜/回放
    out2 = _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    assert out2["review_status"] == "approved"
    assert [x["record_id"] for x in
            ch_api.get_leaderboard(d["id"])["entries"]] == [s["record_id"]]
    assert ch_api.get_submission(s["record_id"])["replayable"] is True


def test_appeal_after_revoke_routes_to_final_level():
    d = _create()
    s, _ = _submit(d["id"])
    _review(s["record_id"], "approve", token=REV_TOKEN, level=1)
    _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    _revoke(s["record_id"])
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    ap = _appeal(s["record_id"], reason="撤销有误")
    # 撤销发生在终审后 → 申诉回到终审级
    assert ap["target_level"] == 2 and _row(s["record_id"]).review_level == 2
    q2 = ch_api.review_queue(level=2)["submissions"]
    assert any(x["record_id"] == s["record_id"] for x in q2)
    # 初审级审核人无权处理终审级申诉
    with pytest.raises(HTTPException) as e:
        _review(s["record_id"], "approve", token=REV_TOKEN)
    assert e.value.status_code == 403
    # 终审级推翻 → 恢复上榜/回放/解锁
    out = _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    assert out["review_status"] == "approved"
    assert out["effects"]["leaderboard"]["on_board"] is True
    assert ch_api.get_leaderboard(d["id"])["entries"]
    # 申诉单记录为推翻，时间线锚定终审级
    assert ch_api.appeals(status="overturn", level=2)["appeals"][0]["appeal_id"] == ap["appeal_id"]


def test_legacy_pipeline_appeal_still_uses_moderator_queue():
    """未配置审核链的旧版挑战：行为完全保持——申诉统一进复核员队列。"""
    d = ch_api.create_challenge(ch_api.CreateChallengeIn(
        title="旧版挑战", **_def()))
    s, _ = _submit(d["id"])
    _review(s["record_id"], "reject")  # 缺省内置初审员
    ap = _appeal(s["record_id"])
    assert ap["target_level"] is None
    assert ch_api.appeals()["appeals"][0]["appeal_id"] == ap["appeal_id"]
    # 普通审核接口拒绝处理（须走复核裁决）
    with pytest.raises(HTTPException) as e:
        _review(s["record_id"], "approve")
    assert e.value.status_code == 409
    dec = _decide(ap["appeal_id"], "overturn")
    assert dec["review_status"] == "approved"
    assert ch_api.get_leaderboard(d["id"])["entries"]


# ---------- 撤销 / 恢复在多级链路下仍同步排行榜、回放与解锁 ----------

def test_revoke_restore_under_pipeline_syncs_board_replay_unlock():
    a = _create(title="前置A")
    dep = ch_api.create_challenge(ch_api.CreateChallengeIn(
        title="依赖", author="测试员", **_def(),
        unlock_rule={"type": "challenge", "challenge_id": a["id"]},
        review_pipeline=None))
    s, _ = _submit(a["id"])
    _review(s["record_id"], "approve", token=REV_TOKEN, level=1)
    _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    assert next(c for c in ch_api.list_challenges()["challenges"]
                if c["id"] == dep["id"])["unlocked"] is True
    # 撤销：出榜 + 关回放 + 级联回收解锁
    out = _revoke(s["record_id"])
    assert out["review_status"] == "revoked"
    assert out["effects"]["leaderboard"]["on_board"] is False
    assert out["effects"]["replayable"] == {"before": True, "after": False}
    changes = {c["challenge_id"]: c["unlocked"]
               for c in out["effects"]["unlock_changes"]}
    assert changes.get(dep["id"]) is False
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    # 恢复：重新上榜/回放/解锁
    rec = _restore(s["record_id"])
    assert rec["review_status"] == "approved"
    assert rec["effects"]["leaderboard"]["on_board"] is True
    assert ch_api.get_submission(s["record_id"])["replayable"] is True
    assert next(c for c in ch_api.list_challenges()["challenges"]
                if c["id"] == dep["id"])["unlocked"] is True


# ---------- 事件链路可追溯 ----------

def test_timeline_records_stage_transitions():
    d = _create()
    s, _ = _submit(d["id"])
    _review(s["record_id"], "reject", token=REV_TOKEN, level=1)
    _appeal(s["record_id"], ap_id="tl-ap")
    _review(s["record_id"], "approve", token=REV_TOKEN, level=1)
    _review(s["record_id"], "approve", token=MOD_TOKEN, level=2)
    tl = ch_api.get_timeline(s["record_id"])
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == ["submit", "review_reject", "appeal",
                     "appeal_overturn", "review_advance", "review_approve"]
    adv = next(e for e in tl["events"] if e["kind"] == "review_advance")
    assert adv["detail"] == {"from_level": 1, "from_stage": "社区初审",
                             "to_level": 2, "to_stage": "官方终审", "note": ""}
    fin = next(e for e in tl["events"] if e["kind"] == "review_approve")
    assert fin["detail"]["level"] == 2 and fin["detail"]["total_levels"] == 2
    assert tl["review_pipeline"]["levels"] == 2
    ap_view = tl["appeals"][0]
    assert ap_view["target_level"] == 1 and ap_view["target_stage"] == "社区初审"
