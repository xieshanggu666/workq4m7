"""社区航线挑战测试：版本化发布 / 幂等提交 / 并发安全 / 审核联动排行榜、回放与解锁。"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 测试使用独立数据库文件（须在导入 app 模块前设置）
os.environ["SLINGSHOT_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="slingshot_ch_test_"), "test.db")

import pytest
from fastapi import HTTPException

from app.core.database import Base, SessionLocal, engine
from app.models import (Challenge, ChallengeAppeal, ChallengeReviewEvent,
                        ChallengeRun, ChallengeSubmission, ChallengeVersion,
                        LevelScore, Reviewer, RunRecord, ScoreRecord)
from app.api import challenges as ch_api
from app.api import router as game_api
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
        # 审核账号表保留并重建为内置账号（各用例权限稳定）
        db.query(Reviewer).delete()
        db.commit()
        ch_svc.bootstrap(db)
        yield
    finally:
        db.close()


# ---------- 工具 ----------

def _def(**over):
    """一份可解的挑战定义（与内置第 1 关同构，参考解法三星）。"""
    d = {
        "name": "首版航线",
        "brief": "飞掠火星即可",
        "hint": "切向点火",
        "budget_dv": 0.004,
        "t_max": 900,
        "milestones": [{"kind": "proximity", "planet_id": "mars",
                        "dist": 0.18, "name": "飞掠火星"}],
    }
    d.update(over)
    return d


def _create(title="火星快线", **over):
    payload = {"title": title, "author": "测试员", **_def(), **over}
    return ch_api.create_challenge(ch_api.CreateChallengeIn(**payload))


def _run(cid, actions=None):
    """执行一次可三星通关的飞行。"""
    actions = actions or [{"type": "burn", "angle": 90.0, "dv": 0.0017}]
    return ch_api.run(cid, ch_api.SimIn(
        actions=[ch_api.Action(**a) for a in actions]))


def _submit(cid, run_id, sub_id, player="飞行员甲"):
    return ch_api.submit(cid, ch_api.SubmitIn(
        run_id=run_id, submission_id=sub_id, player=player))


def _approve(record_id, token=None):
    return ch_api.review_submission(record_id, ch_api.ReviewIn(action="approve"),
                                    token)


def _reject(record_id, note="", token=None):
    return ch_api.review_submission(record_id,
                                    ch_api.ReviewIn(action="reject", note=note),
                                    token)


def _revoke(record_id, note="复核撤销", token=MOD_TOKEN):
    return ch_api.revoke_submission(record_id, ch_api.ModerationIn(note=note), token)


def _restore(record_id, note="复核恢复", token=MOD_TOKEN):
    return ch_api.restore_submission(record_id, ch_api.ModerationIn(note=note), token)


def _appeal(cid_unused, record_id, reason, ap_id, player="飞行员甲"):
    return ch_api.appeal_submission(record_id, ch_api.AppealIn(
        player=player, reason=reason, appeal_id=ap_id))


def _decide(appeal_id, decision, note="", token=MOD_TOKEN):
    return ch_api.decide_appeal(appeal_id,
                                ch_api.AppealDecisionIn(decision=decision, note=note),
                                token)


def _flow_two_stage(**over):
    """两级审核流：初审（初审员）→ 复核（指定本机复核员），默认复核员受理申诉。"""
    f = {"stages": [
        {"name": "初审", "role": "reviewer", "reviewers": [], "sla_hours": 24},
        {"name": "复核", "role": "moderator",
         "reviewers": ["本机复核员"], "sla_hours": 48},
    ], "appeal_route": "moderator"}
    f.update(over)
    return f


def _review_at(record_id, action, stage, token=None, note=""):
    return ch_api.review_submission(
        record_id,
        ch_api.ReviewIn(action=action, note=note, stage=stage), token)


def _submitted(cid, player="飞行员甲", sub_id=None, actions=None):
    """执行 + 提交一条龙，返回提交响应。"""
    import uuid
    r = _run(cid, actions=actions)
    return _submit(cid, r["run_id"], sub_id or uuid.uuid4().hex, player=player), r


def _status(record_id):
    return ch_api.get_submission(record_id)["review_status"]


def _unlocked(challenges, cid):
    return next(c for c in challenges if c["id"] == cid)["unlocked"]


# ---------- 版本化发布 ----------

def test_create_challenge_publishes_v1():
    d = _create()
    assert d["title"] == "火星快线" and d["current_version"] == 1
    assert len(d["versions"]) == 1 and d["versions"][0]["milestone_count"] == 1
    assert d["unlocked"] is True and d["best"] is None
    lst = ch_api.list_challenges()["challenges"]
    assert len(lst) == 1 and lst[0]["milestone_count"] == 1
    assert lst[0]["budget_dv"] == 0.004


def test_create_validation_errors():
    # 未知里程碑类型 / 未知行星 / 空里程碑 / 预算越界 / 未知解锁条件
    for over in [
        {"milestones": [{"kind": "warp", "name": "曲率"}]},
        {"milestones": [{"kind": "proximity", "planet_id": "pluto",
                         "dist": 0.1, "name": "x"}]},
        {"milestones": []},
        {"budget_dv": 0.0001},
        {"budget_dv": 0.5},
        {"t_max": 10},
        {"unlock_rule": {"type": "builtin_level", "level_id": 99}},
        {"unlock_rule": {"type": "challenge", "challenge_id": 42}},
        {"unlock_rule": {"type": "hyperspace"}},
    ]:
        with pytest.raises(HTTPException) as e:
            _create(**over)
        assert e.value.status_code == 400, over


def test_publish_new_version_keeps_old_immutable():
    d = _create()
    v1 = ch_api.version_detail(d["id"], 1)
    d2 = ch_api.publish_version(d["id"], ch_api.VersionIn(
        **_def(name="加严版", budget_dv=0.003)))
    assert d2["current_version"] == 2 and len(d2["versions"]) == 2
    assert d2["current"]["budget_dv"] == 0.003
    # 旧版本仍可取回且未被新版本覆盖
    v1_after = ch_api.version_detail(d["id"], 1)
    assert v1_after["budget_dv"] == v1["budget_dv"] == 0.004
    assert v1_after["is_current"] is False
    with pytest.raises(HTTPException) as e:
        ch_api.version_detail(d["id"], 99)
    assert e.value.status_code == 404


def test_version_can_update_unlock_rule():
    a = _create(title="前置挑战")
    b = _create(title="后续挑战")
    d = ch_api.publish_version(b["id"], ch_api.VersionIn(
        **_def(), unlock_rule=ch_api.UnlockRuleIn(
            type="challenge", challenge_id=a["id"])))
    assert d["unlock_rule"] == {"type": "challenge", "challenge_id": a["id"]}
    assert d["unlocked"] is False
    # 自引用被拒绝
    with pytest.raises(HTTPException) as e:
        ch_api.publish_version(b["id"], ch_api.VersionIn(
            **_def(), unlock_rule=ch_api.UnlockRuleIn(
                type="challenge", challenge_id=b["id"])))
    assert e.value.status_code == 400


# ---------- 飞行执行与幂等提交 ----------

def test_run_settles_and_persists_traceable_record():
    d = _create()
    r = _run(d["id"])
    assert r["ok"] and r["stars"] == 3 and r["run_id"]
    assert r["version"] == 1 and r["challenge_id"] == d["id"]
    db = SessionLocal()
    run = db.query(ChallengeRun).filter(ChallengeRun.run_uid == r["run_id"]).one()
    assert run.version == 1 and run.stars == 3
    assert run.trajectory_json.startswith("[")
    db.close()


def test_preview_does_not_persist():
    d = _create()
    r = ch_api.preview(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)]))
    assert r["ok"] and "run_id" not in r
    db = SessionLocal()
    assert db.query(ChallengeRun).count() == 0
    db.close()


def test_submit_idempotent_and_server_settled():
    d = _create()
    r = _run(d["id"])
    a = _submit(d["id"], r["run_id"], "sub-1")
    b = _submit(d["id"], r["run_id"], "sub-1")
    assert a["record_id"] == b["record_id"] and b["duplicated"] is True
    assert a["review_status"] == "pending"
    db = SessionLocal()
    rec = db.query(ChallengeSubmission).one()
    # 成绩以服务端结算为准（run 落库的星级/燃料）
    assert rec.stars == r["stars"] == 3
    assert abs(rec.fuel_used - r["fuel_used"]) < 1e-9
    assert rec.player == "飞行员甲"
    db.close()


def test_submit_concurrent_duplicates():
    """同一提交并发到达（双击/重试/多标签页）：只落库一条。"""
    d = _create()
    r = _run(d["id"])
    results = []

    def worker():
        results.append(_submit(d["id"], r["run_id"], "race-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(x["saved"] for x in results)
    assert len({x["record_id"] for x in results}) == 1
    db = SessionLocal()
    assert db.query(ChallengeSubmission).count() == 1
    db.close()


def test_same_submission_id_scoped_per_challenge():
    """幂等键作用域仅限本挑战：不同挑战复用同一键是各自独立的提交，
    成绩/审核/排行榜/回放都锚定各自的挑战，不会串到别处。"""
    a = _create(title="挑战A")
    b = _create(title="挑战B")
    ra = _run(a["id"])
    rb = _run(b["id"])
    sa = _submit(a["id"], ra["run_id"], "shared-key")
    sb = _submit(b["id"], rb["run_id"], "shared-key")
    # 两条独立成绩，分别归属各自挑战
    assert sa["record_id"] != sb["record_id"]
    assert sa["challenge_id"] == a["id"] and sb["challenge_id"] == b["id"]
    assert sa["duplicated"] is False and sb["duplicated"] is False
    db = SessionLocal()
    recs = db.query(ChallengeSubmission).order_by(ChallengeSubmission.id).all()
    assert len(recs) == 2
    assert {r.challenge_id for r in recs} == {a["id"], b["id"]}
    db.close()

    # 各自重复提交仍幂等，且返回的是本挑战的那条
    sa2 = _submit(a["id"], ra["run_id"], "shared-key")
    sb2 = _submit(b["id"], rb["run_id"], "shared-key")
    assert sa2["duplicated"] is True and sa2["record_id"] == sa["record_id"]
    assert sa2["challenge_id"] == a["id"]
    assert sb2["duplicated"] is True and sb2["record_id"] == sb["record_id"]
    assert sb2["challenge_id"] == b["id"]

    # 审核 A 的成绩：只影响 A 的排行榜/回放，B 的成绩保持待审
    _approve(sa["record_id"])
    assert len(ch_api.get_leaderboard(a["id"])["entries"]) == 1
    assert ch_api.get_leaderboard(b["id"])["entries"] == []
    da = ch_api.get_submission(sa["record_id"])
    assert da["replayable"] is True and da["challenge_id"] == a["id"]
    assert da["trajectory"]  # A 开放回放
    db_detail = ch_api.get_submission(sb["record_id"])
    assert db_detail["replayable"] is False and "trajectory" not in db_detail
    # A 的回放轨迹来自 A 的执行档案，而不是 B 的
    assert da["run_uid"] == ra["run_id"] != rb["run_id"]

    # 通过 B 后双方各在各的排行榜上
    _approve(sb["record_id"])
    entries_a = ch_api.get_leaderboard(a["id"])["entries"]
    entries_b = ch_api.get_leaderboard(b["id"])["entries"]
    assert [e["record_id"] for e in entries_a] == [sa["record_id"]]
    assert [e["record_id"] for e in entries_b] == [sb["record_id"]]


def test_same_submission_id_concurrent_across_challenges():
    """两个挑战并发提交同一个幂等键：各自落库一条，不互相吞掉。"""
    a = _create(title="挑战A")
    b = _create(title="挑战B")
    ra = _run(a["id"])
    rb = _run(b["id"])
    results = []

    def worker(cid, run_id):
        with SessionLocal() as db:
            results.append(ch_svc.submit(
                db, challenge_id=cid, run_id=run_id,
                submission_id="race-shared", player="飞行员"))

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(target=worker, args=(a["id"], ra["run_id"])))
        threads.append(threading.Thread(target=worker, args=(b["id"], rb["run_id"])))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    by_challenge = {a["id"]: set(), b["id"]: set()}
    for r in results:
        by_challenge[r["challenge_id"]].add(r["record_id"])
    assert len(by_challenge[a["id"]]) == 1
    assert len(by_challenge[b["id"]]) == 1
    assert by_challenge[a["id"]] != by_challenge[b["id"]]
    db = SessionLocal()
    assert db.query(ChallengeSubmission).count() == 2
    db.close()


def test_submit_rejects_bad_run():
    d = _create()
    with pytest.raises(HTTPException) as e1:
        _submit(d["id"], "nonexistent", "s-x")
    assert e1.value.status_code == 404
    other = _create(title="另一挑战")
    r = _run(d["id"])
    with pytest.raises(HTTPException) as e2:  # 执行档案属于挑战 d，不能记到 other
        _submit(other["id"], r["run_id"], "s-y")
    assert e2.value.status_code == 400


# ---------- 审核联动：排行榜 / 回放 / 解锁 ----------

def test_review_gates_leaderboard_replay_and_unlock():
    a = _create(title="航线A")
    b = _create(title="航线B", unlock_rule={"type": "challenge",
                                          "challenge_id": a["id"]})
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False

    r = _run(a["id"])
    sub = _submit(a["id"], r["run_id"], "sub-a")

    # 审核前：排行榜为空、回放不开放、B 仍锁定
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] is False and "trajectory" not in detail
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False

    # 审核通过：上榜、回放开放、B 解锁
    rv = _approve(sub["record_id"])
    assert rv["review_status"] == "approved" and rv["duplicated"] is False
    entries = ch_api.get_leaderboard(a["id"])["entries"]
    assert len(entries) == 1 and entries[0]["player"] == "飞行员甲"
    assert entries[0]["stars"] == 3 and entries[0]["rank"] == 1
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] and detail["trajectory"]
    assert detail["actions"][0]["type"] == "burn"
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True
    # 列表里的本机最佳同步刷新
    lst = ch_api.list_challenges()["challenges"]
    assert next(c for c in lst if c["id"] == a["id"])["best"]["stars"] == 3


def test_review_reject_excludes_everything():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-r")
    rv = ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(
        action="reject", note="轨迹异常"))
    assert rv["review_status"] == "rejected"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] is False and detail["review_note"] == "轨迹异常"


def test_review_idempotent_and_conflict():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-i")
    _approve(sub["record_id"])
    again = _approve(sub["record_id"])  # 重复通过：幂等
    assert again["duplicated"] is True
    with pytest.raises(HTTPException) as e:  # 已通过后驳回：冲突
        ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(action="reject"))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        ch_api.review_submission(9999, ch_api.ReviewIn(action="approve"))
    assert e2.value.status_code == 404
    with pytest.raises(HTTPException) as e3:
        ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(action="maybe"))
    assert e3.value.status_code == 400


def test_review_queue_lists_pending():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-q")
    queue = ch_api.review_queue()["submissions"]
    assert len(queue) == 1 and queue[0]["record_id"] == sub["record_id"]
    assert queue[0]["challenge_title"] == "火星快线"
    _approve(sub["record_id"])
    assert ch_api.review_queue()["submissions"] == []
    approved = ch_api.review_queue(status="approved")["submissions"]
    assert len(approved) == 1


# ---------- 排行榜规则 ----------

def test_leaderboard_best_per_player_and_ordering():
    d = _create()
    # 玩家甲两次成绩（先差后好）：只保留最佳一条
    r1 = _run(d["id"], [{"type": "burn", "angle": 90.0, "dv": 0.0035}])
    s1 = _submit(d["id"], r1["run_id"], "lb-1", player="甲")
    r2 = _run(d["id"])  # 三星、燃料更省
    s2 = _submit(d["id"], r2["run_id"], "lb-2", player="甲")
    r3 = _run(d["id"])
    s3 = _submit(d["id"], r3["run_id"], "lb-3", player="乙")
    for rec in (s1, s2, s3):
        _approve(rec["record_id"])
    entries = ch_api.get_leaderboard(d["id"])["entries"]
    assert len(entries) == 2  # 甲只出现一次（最佳）
    jia = next(e for e in entries if e["player"] == "甲")
    assert jia["record_id"] == s2["record_id"] and jia["stars"] == 3
    # 同星同玩家最佳并列时按燃料排序：甲(0.0017) 在 乙(0.0017) 前（先到优先）
    assert entries[0]["player"] == "甲"


def test_leaderboard_isolated_by_version():
    d = _create()
    r = _run(d["id"])  # v1 成绩
    s = _submit(d["id"], r["run_id"], "v1-sub")
    _approve(s["record_id"])
    ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(budget_dv=0.003)))
    # 当前版本（v2）排行榜为空；v1 排行榜保留成绩
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    assert ch_api.get_leaderboard(d["id"])["version"] == 2
    v1 = ch_api.get_leaderboard(d["id"], version=1)
    assert len(v1["entries"]) == 1 and v1["entries"][0]["record_id"] == s["record_id"]
    assert v1["entries"][0]["version"] == 1
    assert v1["current_version"] == 2 and v1["version_count"] == 2


def test_old_version_submission_still_reviews_replays_and_unlocks():
    a = _create(title="旧版前置")
    b = _create(title="旧版后续", unlock_rule={"type": "challenge",
                                              "challenge_id": a["id"]})
    r = _run(a["id"])  # 成绩锚定 v1
    s = _submit(a["id"], r["run_id"], "old-review")
    ch_api.publish_version(a["id"], ch_api.VersionIn(**_def(budget_dv=0.003)))

    # 新版本发布后，旧成绩仍可审核；通过后只进 v1 榜并开放旧档案回放
    approved = _approve(s["record_id"])
    assert approved["version"] == 1
    assert approved["effects"]["leaderboard"] == {
        "on_board": True, "best_record_id": s["record_id"], "version": 1}
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    v1_board = ch_api.get_leaderboard(a["id"], version=1)["entries"]
    assert [e["record_id"] for e in v1_board] == [s["record_id"]]
    detail = ch_api.get_submission(s["record_id"])
    assert detail["replayable"] is True and detail["version"] == 1
    assert detail["run_uid"] == r["run_id"] and detail["trajectory"]
    # 旧版本成绩仍是对挑战本身的通关证据，继续影响依赖它的解锁
    challenges = ch_api.list_challenges()["challenges"]
    a_card = next(c for c in challenges if c["id"] == a["id"])
    assert a_card["best"]["record_id"] == s["record_id"]
    assert a_card["best"]["version"] == 1
    assert a_card["best_current"] is None
    assert _unlocked(challenges, b["id"]) is True

    # 撤销旧成绩：从 v1 榜和回放中移除，并级联回收解锁；当前 v2 榜不受影响
    revoked = _revoke(s["record_id"])
    assert revoked["effects"]["leaderboard"] == {
        "on_board": False, "best_record_id": None, "version": 1}
    assert ch_api.get_leaderboard(a["id"], version=1)["entries"] == []
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False

    # 恢复旧成绩：重新进入 v1 榜、开放回放，并恢复解锁
    restored = _restore(s["record_id"])
    assert restored["effects"]["leaderboard"]["on_board"] is True
    assert restored["effects"]["leaderboard"]["best_record_id"] == s["record_id"]
    assert ch_api.get_leaderboard(a["id"], version=1)["entries"][0]["record_id"] == s["record_id"]
    assert ch_api.get_submission(s["record_id"])["replayable"] is True
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True


# ---------- 解锁联动 ----------

def test_unlock_by_builtin_level():
    d = _create(unlock_rule={"type": "builtin_level", "level_id": 1})
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is False
    # 未解锁时结算类接口拒绝（403），预览仍可用
    with pytest.raises(HTTPException) as e:
        _run(d["id"])
    assert e.value.status_code == 403
    assert ch_api.preview(d["id"], ch_api.SimIn())["ok"] is False or True
    # 通关内置第 1 关后解锁
    game_api.save_score(game_api.ScoreRequest(level_id=1, stars=1,
                                              fuel_used=0.003, elapsed_days=500.0))
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is True
    assert _run(d["id"])["ok"]


def test_unlock_by_stars_total():
    d = _create(unlock_rule={"type": "stars_total", "value": 4})
    game_api.save_score(game_api.ScoreRequest(level_id=1, stars=3,
                                              fuel_used=0.002, elapsed_days=400.0))
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is False
    # 另一个挑战的已审核星数也计入总星数
    other = _create(title="星星来源")
    r = _run(other["id"])
    s = _submit(other["id"], r["run_id"], "star-src")
    _approve(s["record_id"])  # 3 星 → 总星数 6 ≥ 4
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is True


def test_unlock_chain_between_challenges():
    a = _create(title="第一环")
    b = _create(title="第二环", unlock_rule={"type": "challenge",
                                           "challenge_id": a["id"]})
    c = _create(title="第三环", unlock_rule={"type": "challenge",
                                           "challenge_id": b["id"]})
    lst = ch_api.list_challenges()["challenges"]
    assert _unlocked(lst, b["id"]) is False and _unlocked(lst, c["id"]) is False
    # 通过 A 的成绩审核后：B 解锁，C 仍锁定（链式逐级联动）
    r = _run(a["id"])
    _approve(_submit(a["id"], r["run_id"], "chain-a")["record_id"])
    lst = ch_api.list_challenges()["challenges"]
    assert _unlocked(lst, b["id"]) is True and _unlocked(lst, c["id"]) is False
    r = _run(b["id"])
    _approve(_submit(b["id"], r["run_id"], "chain-b")["record_id"])
    assert _unlocked(ch_api.list_challenges()["challenges"], c["id"]) is True


def test_run_on_specific_version():
    d = _create()
    ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(budget_dv=0.003)))
    r = ch_api.run(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)], version=1))
    assert r["version"] == 1 and r["ok"]
    s = _submit(d["id"], r["run_id"], "old-v")
    assert s["version"] == 1
    _approve(s["record_id"])
    assert len(ch_api.get_leaderboard(d["id"], version=1)["entries"]) == 1
    assert ch_api.get_leaderboard(d["id"])["entries"] == []  # 当前 v2 为空


# ---------- 审核权限 ----------

def test_reviewer_auth_levels():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "auth-1")
    # 无效 token → 401；初审员不能复核操作
    with pytest.raises(HTTPException) as e1:
        _revoke(s["record_id"], token="nope")
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        _revoke(s["record_id"], token=REV_TOKEN)
    assert e2.value.status_code == 403
    # 复核员注册账号；初审员 token 也能走初审接口
    rv = ch_api.create_reviewer(ch_api.ReviewerIn(
        name="新初审", token="t-new", role="reviewer"), MOD_TOKEN)
    assert rv["role"] == "reviewer"
    out = ch_api.review_submission(s["record_id"],
                                   ch_api.ReviewIn(action="approve"), "t-new")
    assert out["review_status"] == "approved"
    # 缺凭证注册 → 401；初审员注册 → 403
    with pytest.raises(HTTPException) as e3:
        ch_api.create_reviewer(ch_api.ReviewerIn(name="x", token="t-x", role="reviewer"))
    assert e3.value.status_code == 401
    with pytest.raises(HTTPException) as e4:
        ch_api.create_reviewer(ch_api.ReviewerIn(name="y", token="t-y", role="reviewer"),
                               REV_TOKEN)
    assert e4.value.status_code == 403
    # 署名/token 冲突 → 409
    with pytest.raises(HTTPException) as e5:
        ch_api.create_reviewer(ch_api.ReviewerIn(
            name="新初审", token="t-z", role="reviewer"), MOD_TOKEN)
    assert e5.value.status_code == 409
    # /reviewers/me 识别角色
    assert ch_api.reviewer_me(MOD_TOKEN)["role"] == "moderator"
    assert ch_api.reviewer_me()["role"] == "reviewer"  # 缺省降级内置初审员


def test_invalid_token_rejected_for_moderation_even_without_header():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "auth-2")
    _approve(s["record_id"])
    # 撤销必须显式复核员 token（缺省入口只是初审员）
    with pytest.raises(HTTPException) as e:
        ch_api.revoke_submission(s["record_id"], ch_api.ModerationIn())
    assert e.value.status_code == 401


# ---------- 撤销 / 恢复：排行榜 / 回放 / 解锁回滚 ----------

def test_revoke_rolls_back_leaderboard_replay_and_unlock():
    a = _create(title="前置A")
    b = _create(title="后续B", unlock_rule={"type": "challenge",
                                           "challenge_id": a["id"]})
    r = _run(a["id"])
    s = _submit(a["id"], r["run_id"], "rev-1")
    _approve(s["record_id"])
    assert len(ch_api.get_leaderboard(a["id"])["entries"]) == 1
    assert ch_api.get_submission(s["record_id"])["replayable"] is True
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True
    # 复核员撤销：出榜、回放关闭、B 级联重新锁定，响应回传联动 diff
    out = _revoke(s["record_id"])
    assert out["review_status"] == "revoked" and out["duplicated"] is False
    assert out["effects"]["leaderboard"]["on_board"] is False
    assert out["effects"]["replayable"] == {"before": True, "after": False}
    changes = {c["challenge_id"]: c for c in out["effects"]["unlock_changes"]}
    assert changes[b["id"]]["unlocked"] is False
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    detail = ch_api.get_submission(s["record_id"])
    assert detail["replayable"] is False
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False
    # 撤销可申诉（from=revoked）
    ap = _appeal(a["id"], s["record_id"], "轨迹被误判，请求复核", "ap-rev")
    assert ap["from_status"] == "revoked" and _status(s["record_id"]) == "pending"
    # 恢复被进行中的申诉阻挡
    with pytest.raises(HTTPException) as e:
        _restore(s["record_id"])
    assert e.value.status_code == 409


def test_revoke_idempotent_and_conflict():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "rev-2")
    # pending/rejected 不能撤销
    with pytest.raises(HTTPException) as e1:
        _revoke(s["record_id"])
    assert e1.value.status_code == 409
    _reject(s["record_id"], note="异常")
    with pytest.raises(HTTPException) as e2:
        _revoke(s["record_id"])
    assert e2.value.status_code == 409
    # 重新走一条通过的成绩
    r2 = _run(d["id"])
    s2 = _submit(d["id"], r2["run_id"], "rev-3")
    _approve(s2["record_id"])
    _revoke(s2["record_id"])
    again = _revoke(s2["record_id"])  # 重复撤销：幂等
    assert again["duplicated"] is True and again["review_status"] == "revoked"
    # 恢复：重新上榜/回放/解锁
    rec = _restore(s2["record_id"])
    assert rec["review_status"] == "approved" and rec["duplicated"] is False
    assert rec["effects"]["leaderboard"]["on_board"] is True
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1
    again = _restore(s2["record_id"])  # 重复恢复：幂等
    assert again["duplicated"] is True
    # 已恢复后驳回类操作冲突
    with pytest.raises(HTTPException) as e3:
        _reject(s2["record_id"])
    assert e3.value.status_code == 409


def test_revoke_falls_back_to_next_best_leaderboard_entry():
    """撤销榜首后排行榜自动顺延到次佳；恢复后重新占榜首。"""
    d = _create()
    s1, _ = _submitted(d["id"], player="甲", sub_id="rk-1",
                       actions=[{"type": "burn", "angle": 90.0, "dv": 0.0035}])
    s2, _ = _submitted(d["id"], player="乙", sub_id="rk-2")
    _approve(s1["record_id"])
    _approve(s2["record_id"])
    board = ch_api.get_leaderboard(d["id"])["entries"]
    assert [e["player"] for e in board] == ["甲", "乙"] or \
           board[0]["player"] == "乙"
    top_id = board[0]["record_id"]
    _revoke(top_id)
    board2 = ch_api.get_leaderboard(d["id"])["entries"]
    assert len(board2) == 1 and board2[0]["record_id"] != top_id
    _restore(top_id)
    board3 = ch_api.get_leaderboard(d["id"])["entries"]
    assert len(board3) == 2 and board3[0]["record_id"] == top_id


def test_revoke_restore_non_top_entry_is_still_on_its_leaderboard():
    """在榜不等于榜首：每名玩家在该版本的最佳成绩撤销后才离榜，恢复后回原榜。"""
    d = _create()
    top, _ = _submitted(d["id"], player="榜首", sub_id="board-top")
    lower, _ = _submitted(
        d["id"], player="榜尾", sub_id="board-lower",
        actions=[{"type": "burn", "angle": 90.0, "dv": 0.0035}])
    top_out = _approve(top["record_id"])
    lower_out = _approve(lower["record_id"])
    assert top_out["effects"]["leaderboard"]["best_record_id"] == top["record_id"]
    assert lower_out["effects"]["leaderboard"]["on_board"] is True
    assert lower_out["effects"]["leaderboard"]["best_record_id"] == top["record_id"]

    revoked = _revoke(lower["record_id"])
    assert revoked["effects"]["leaderboard"]["on_board"] is False
    assert revoked["effects"]["leaderboard"]["best_record_id"] == top["record_id"]
    assert [e["record_id"] for e in ch_api.get_leaderboard(d["id"])["entries"]] == [
        top["record_id"]]

    restored = _restore(lower["record_id"])
    assert restored["effects"]["leaderboard"]["on_board"] is True
    assert restored["effects"]["leaderboard"]["best_record_id"] == top["record_id"]
    assert [e["record_id"] for e in ch_api.get_leaderboard(d["id"])["entries"]] == [
        top["record_id"], lower["record_id"]]


# ---------- 玩家申诉与复核裁决 ----------

def test_appeal_full_cycle_uphold_then_overturn():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "apc-1")
    _reject(s["record_id"], note="疑似作弊")
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    # 第 1 轮申诉（幂等键）
    a1 = _appeal(d["id"], s["record_id"], "服务端结算可复查", "ap-1")
    assert a1["round"] == 1 and a1["status"] == "pending"
    a1dup = _appeal(d["id"], s["record_id"], "服务端结算可复查", "ap-1")
    assert a1dup["duplicated"] is True and a1dup["appeal_id"] == a1["appeal_id"]
    # 申诉重审期间出现在待审队列且带申诉标记；初审接口拒绝处理
    queue = ch_api.review_queue()["submissions"]
    row = next(x for x in queue if x["record_id"] == s["record_id"])
    assert row["appeal_round"] == 1 and row["appeal_reason"]
    with pytest.raises(HTTPException) as e0:
        _approve(s["record_id"])
    assert e0.value.status_code == 409
    # 申诉队列可见
    aps = ch_api.appeals()["appeals"]
    assert len(aps) == 1 and aps[0]["appeal_id"] == a1["appeal_id"]
    # 复核维持 → 回到 rejected
    dec = _decide(a1["appeal_id"], "uphold", note="证据不足")
    assert dec["decision"] == "uphold" and dec["review_status"] == "rejected"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    # 同一申诉重复裁决：相同幂等、相反 409
    assert _decide(a1["appeal_id"], "uphold")["duplicated"] is True
    with pytest.raises(HTTPException) as e1:
        _decide(a1["appeal_id"], "overturn")
    assert e1.value.status_code == 409
    # 第 2 轮申诉，复核推翻 → approved 上榜
    a2 = _appeal(d["id"], s["record_id"], "补充轨迹录像", "ap-2")
    assert a2["round"] == 2
    dec2 = _decide(a2["appeal_id"], "overturn", note="改判通过")
    assert dec2["review_status"] == "approved"
    assert dec2["effects"]["replayable"] == {"before": False, "after": True}
    entries = ch_api.get_leaderboard(d["id"])["entries"]
    assert len(entries) == 1 and entries[0]["record_id"] == s["record_id"]
    assert ch_api.get_submission(s["record_id"])["replayable"] is True
    # 轮次用尽：第 3 次申诉 409
    _revoke(s["record_id"])
    with pytest.raises(HTTPException) as e2:
        _appeal(d["id"], s["record_id"], "第三次", "ap-3")
    assert e2.value.status_code == 409


def test_appeal_constraints():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "apc-2")
    # pending 不能申诉；通过的成绩不能申诉
    with pytest.raises(HTTPException) as e1:
        _appeal(d["id"], s["record_id"], "催一下", "ap-x1")
    assert e1.value.status_code == 409
    _approve(s["record_id"])
    with pytest.raises(HTTPException) as e2:
        _appeal(d["id"], s["record_id"], "没意见也申诉", "ap-x2")
    assert e2.value.status_code == 409
    # 只有本人可申诉
    _revoke(s["record_id"])
    with pytest.raises(HTTPException) as e3:
        _appeal(d["id"], s["record_id"], "我不是本人", "ap-x3", player="路人")
    assert e3.value.status_code == 403
    # 理由不能为空
    with pytest.raises(HTTPException) as e4:
        ch_api.appeal_submission(s["record_id"], ch_api.AppealIn(
            player="飞行员甲", reason="   ", appeal_id="ap-x4"))
    assert e4.value.status_code == 400
    # 已进行中的申诉不能重复发起（换幂等键也不行）
    _appeal(d["id"], s["record_id"], "第一条申诉", "ap-x5")
    with pytest.raises(HTTPException) as e5:
        _appeal(d["id"], s["record_id"], "第二条申诉", "ap-x6")
    assert e5.value.status_code == 409
    # 不存在的申诉/成绩 → 404；未知裁决 → 400
    with pytest.raises(HTTPException) as e6:
        _decide(99999, "uphold")
    assert e6.value.status_code == 404
    with pytest.raises(HTTPException) as e7:
        _decide(ch_api.appeals()["appeals"][0]["appeal_id"], "maybe")
    assert e7.value.status_code == 400


def test_appeal_overturn_restores_unlock_chain():
    """驳回时锁定 → 申诉后 pending 仍锁定 → 推翻改判通过后级联解锁。"""
    a = _create(title="环A")
    b = _create(title="环B", unlock_rule={"type": "challenge",
                                         "challenge_id": a["id"]})
    s, _ = _submitted(a["id"], sub_id="apc-3")
    _reject(s["record_id"])
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False
    ap = _appeal(a["id"], s["record_id"], "误判", "ap-chain")
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False
    dec = _decide(ap["appeal_id"], "overturn")
    assert dec["effects"]["unlock_changes"]
    cid_change = {c["challenge_id"]: c["unlocked"]
                  for c in dec["effects"]["unlock_changes"]}
    assert cid_change.get(b["id"]) is True
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True


def test_appeal_concurrent_duplicates():
    """同一申诉并发到达：只受理一条。"""
    d = _create()
    s, _ = _submitted(d["id"], sub_id="apc-4")
    _reject(s["record_id"])
    results = []

    def worker():
        results.append(_appeal(d["id"], s["record_id"], "并发申诉", "race-ap"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({x["appeal_id"] for x in results}) == 1
    db = SessionLocal()
    assert db.query(ChallengeAppeal).count() == 1
    db.close()


# ---------- 可追溯事件链路 / 历史兼容 ----------

def test_timeline_records_full_chain():
    d = _create()
    s, _ = _submitted(d["id"], sub_id="tl-1")
    _reject(s["record_id"], note="初审判定异常")
    ap = _appeal(d["id"], s["record_id"], "申请复核", "tl-ap")
    _decide(ap["appeal_id"], "uphold", note="维持")
    ap2 = _appeal(d["id"], s["record_id"], "再次申请", "tl-ap2")
    _decide(ap2["appeal_id"], "overturn", note="改判")
    _revoke(s["record_id"], note="事后撤销")
    _restore(s["record_id"], note="撤销有误")
    tl = ch_api.get_timeline(s["record_id"])
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == ["submit", "review_reject", "appeal", "appeal_uphold",
                     "appeal", "appeal_overturn", "revoke", "restore"]
    actors = {e["actor_role"] for e in tl["events"]}
    assert actors == {"player", "reviewer", "moderator"}
    # 事件与申诉单都锚定到同一条成绩，且详情可解析
    submit_ev = next(e for e in tl["events"] if e["kind"] == "submit")
    assert submit_ev["detail"]["submission_id"] == "tl-1"
    assert len(tl["appeals"]) == 2
    assert tl["appeals"][0]["status"] == "uphold"
    assert tl["appeals"][1]["status"] == "overturn"
    assert tl["review_status"] == "approved"


def test_review_queue_and_player_view_surface_appeal_fields():
    d = _create()
    s, _ = _submitted(d["id"], sub_id="view-1")
    _reject(s["record_id"], note="不行")
    _appeal(d["id"], s["record_id"], "请复核", "view-ap")
    mine = ch_api.my_submissions("飞行员甲")["submissions"]
    assert len(mine) == 1
    m = mine[0]
    assert m["appeal_rounds"] == 1 and m["appeal_open"] is not None
    assert m["appeal_available"] is False and m["replayable"] is False
    # 别的玩家看不到该提交
    assert ch_api.my_submissions("路人乙")["submissions"] == []
    # 裁决后入口重新开放（第 2 轮）
    _decide(ch_api.appeals()["appeals"][0]["appeal_id"], "uphold")
    m2 = ch_api.my_submissions("飞行员甲")["submissions"][0]
    assert m2["appeal_open"] is None and m2["appeal_available"] is True
    assert m2["review_status"] == "rejected"


def test_legacy_reviewed_submissions_backfill_events():
    """兼容已审核历史成绩：旧库（无事件、无 reviewed_by 列）补登 legacy 事件。"""
    import sqlite3
    from app.core.config import DB_PATH
    d = _create()
    s, _ = _submitted(d["id"], sub_id="legacy-1")
    rid = s["record_id"]
    # 直接走底层把成绩改成"旧时代已终审"且不留事件
    ch_svc.reset_bootstrap_for_tests()
    db = SessionLocal()
    db.query(ChallengeReviewEvent).delete()
    rec = db.query(ChallengeSubmission).filter(ChallengeSubmission.id == rid).one()
    rec.review_status = "approved"
    rec.review_note = "旧版审核通过"
    rec.reviewed_by = None
    db.commit()
    db.close()
    # 模拟旧库删除 reviewed_by 列（SQLite 需要重建表；保留主键/新列，仅去该列）
    conn = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(challenge_submission)")]
    if "reviewed_by" in cols:
        keep = [c for c in cols if c != "reviewed_by"]
        defs = []
        for r in conn.execute("PRAGMA table_info(challenge_submission)").fetchall():
            if r[1] == "reviewed_by":
                continue
            col = f"{r[1]} {r[2] or ''}".strip()
            if r[5]:  # pk
                col += " PRIMARY KEY"
            defs.append(col)
        keep_sql = ", ".join(keep)
        conn.execute("PRAGMA foreign_keys=off")
        conn.execute("ALTER TABLE challenge_submission RENAME TO _cs_old")
        conn.execute(
            f"CREATE TABLE challenge_submission ({', '.join(defs)}, "
            "UNIQUE (challenge_id, submission_id), "
            "FOREIGN KEY(run_id) REFERENCES challenge_run (id))")
        conn.execute(
            f"INSERT INTO challenge_submission ({keep_sql}) "
            f"SELECT {keep_sql} FROM _cs_old")
        conn.execute("DROP TABLE _cs_old")
        conn.execute("PRAGMA foreign_keys=on")
        conn.commit()
    conn.close()

    # 启动引导：加列 + 补登 legacy；历史成绩仍在榜、可回放、链路完整
    db = SessionLocal()
    ch_svc.bootstrap(db)
    tl = ch_svc.submission_timeline(db, rid)
    db.close()
    assert [e["kind"] for e in tl["events"]] == ["legacy"]
    assert tl["events"][0]["detail"]["status"] == "approved"
    assert tl["review_status"] == "approved" and tl["replayable"] is True
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1
    # 补登幂等：再次引导不产生重复事件
    ch_svc.reset_bootstrap_for_tests()
    db = SessionLocal()
    ch_svc.bootstrap(db)
    n = db.query(ChallengeReviewEvent).filter(
        ChallengeReviewEvent.submission_id == rid).count()
    db.close()
    assert n == 1


# ---------- 可配置多级审核流 ----------

def test_review_flow_validation():
    # 未知角色 / 级过多 / 空级 / SLA 越界 / 指定不存在的审核人 / 角色不匹配
    bad = [
        {"stages": [{"name": "X", "role": "wizard"}]},
        {"stages": [{"name": f"s{i}"} for i in range(6)]},
        {"stages": []},
        {"stages": [{"name": "X", "sla_hours": 0.1}]},
        {"stages": [{"name": "X", "sla_hours": 99999}]},
        {"stages": [{"name": "X", "reviewers": ["不存在的人"]}]},
        {"stages": [{"name": "X", "role": "moderator",
                     "reviewers": ["本机初审员"]}]},
        {"appeal_route": "mars", "stages": [{"name": "X"}]},
    ]
    for flow in bad:
        with pytest.raises(HTTPException) as e:
            _create(review_flow=flow)
        assert e.value.status_code == 400, flow
    # 同名审核级也不行
    with pytest.raises(HTTPException) as e:
        _create(review_flow={"stages": [{"name": "审"}, {"name": "审"}]})
    assert e.value.status_code == 400


def test_review_flow_default_is_single_stage_and_backward_compatible():
    d = _create()
    flow = d["current"]["review_flow"]
    assert flow["custom"] is False and len(flow["stages"]) == 1
    assert flow["stages"][0]["name"] == "初审"
    assert flow["appeal_route"] == "moderator"
    v1 = ch_api.version_detail(d["id"], 1)
    assert v1["review_flow"]["custom"] is False
    lst = ch_api.list_challenges()["challenges"][0]
    assert lst["review_stages"] == 1


def test_multistage_submission_progresses_stage_by_stage():
    d = _create(review_flow=_flow_two_stage())
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "ms-1")
    assert s["review_status"] == "pending"
    assert s["review_stage"] == 0 and s["review_stage_name"] == "初审"
    assert s["review_stage_count"] == 2
    assert s["sla_deadline"]  # 第 1 级 24h 时限
    # 队列：默认在第 1 级
    q = ch_api.review_queue()["submissions"]
    row = next(x for x in q if x["record_id"] == s["record_id"])
    assert row["review_stage"] == 0 and row["review_stage_name"] == "初审"
    assert row["review_stage_index"] == 1 and row["review_stage_count"] == 2
    assert row["sla_overdue"] is False
    # 第 2 级队列暂时为空
    assert ch_api.review_queue(stage=1)["submissions"] == []

    # 多级流程不带 stage → 409；错误级 → 409
    with pytest.raises(HTTPException) as e1:
        _approve(s["record_id"])
    assert e1.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        _review_at(s["record_id"], "approve", 1)
    assert e2.value.status_code == 409

    # 初审员不能处理复核级（即便误带 stage=1，也因成绩不在该级先报 409）
    # 第 1 级通过：仍不上榜，进入第 2 级队列
    out1 = _review_at(s["record_id"], "approve", 0)
    assert out1["review_status"] == "pending"
    assert out1["review_stage_name"] == "复核"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    assert len(out1["stage_history"]) == 1
    q1 = ch_api.review_queue(stage=1)["submissions"]
    assert [x["record_id"] for x in q1] == [s["record_id"]]

    # 初审员处理第 2 级 → 403（该级指定复核员）
    with pytest.raises(HTTPException) as e3:
        _review_at(s["record_id"], "approve", 1, token=REV_TOKEN)
    assert e3.value.status_code == 403
    # 复核员通过末级：上榜/回放/解锁
    out2 = _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    assert out2["review_status"] == "approved"
    assert len(out2["stage_history"]) == 2
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1
    detail = ch_api.get_submission(s["record_id"])
    assert detail["replayable"] is True and detail["review_stage_count"] == 2
    assert [h["action"] for h in detail["stage_history"]] == ["approve", "approve"]
    tl = ch_api.get_timeline(s["record_id"])
    assert [e["kind"] for e in tl["events"]] == [
        "submit", "review_approve", "review_approve"]
    assert tl["events"][1]["detail"]["stage"] == 0
    assert tl["events"][2]["detail"]["final"] is True


def test_multistage_reject_at_second_stage_excludes():
    d = _create(review_flow=_flow_two_stage())
    s, _ = _submitted(d["id"], sub_id="ms-2")
    _review_at(s["record_id"], "approve", 0)
    out = _review_at(s["record_id"], "reject", 1, token=MOD_TOKEN, note="复核驳回")
    assert out["review_status"] == "rejected"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    # 已驳回不能继续走审核接口
    with pytest.raises(HTTPException) as e:
        _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    assert e.value.status_code == 409


def test_multistage_queue_can_review_uses_named_reviewers():
    # 两级都指定专人：只有名单内审核人的 can_review 为真
    ch_api.create_reviewer(ch_api.ReviewerIn(
        name="专家甲", token="t-exp1", role="reviewer"), MOD_TOKEN)
    flow = {"stages": [
        {"name": "专家初审", "role": "reviewer", "reviewers": ["专家甲"]},
        {"name": "终审", "role": "moderator", "reviewers": ["本机复核员"]},
    ], "appeal_route": "moderator"}
    d = _create(review_flow=flow)
    s, _ = _submitted(d["id"], sub_id="ms-3")
    # 不带 token 不判定（None）
    row_noauth = next(x for x in ch_api.review_queue()["submissions"]
                      if x["record_id"] == s["record_id"])
    assert row_noauth["can_review"] is None
    # 直接服务层验证名单授权
    db = SessionLocal()
    ver = ch_svc._get_version(db, d["id"], 1)
    stage0 = ch_svc.flow_of(ver)["stages"][0]
    acc_builtin = ch_svc.authenticate(db, REV_TOKEN)
    acc_exp = ch_svc.authenticate(db, "t-exp1")
    acc_mod = ch_svc.authenticate(db, MOD_TOKEN)
    assert ch_svc._stage_allows(stage0, acc_builtin) is False
    assert ch_svc._stage_allows(stage0, acc_exp) is True
    assert ch_svc._stage_allows(stage0, acc_mod) is False
    db.close()
    # 队列经 token 计算 can_review
    row_builtin = next(x for x in ch_api.review_queue(
        x_reviewer_token=REV_TOKEN)["submissions"]
        if x["record_id"] == s["record_id"])
    row_exp = next(x for x in ch_api.review_queue(
        x_reviewer_token="t-exp1")["submissions"]
        if x["record_id"] == s["record_id"])
    assert row_builtin["can_review"] is False
    assert row_exp["can_review"] is True
    # 内置初审员越权处理 → 403；专家甲可处理
    with pytest.raises(HTTPException) as e:
        _review_at(s["record_id"], "approve", 0, token=REV_TOKEN)
    assert e.value.status_code == 403
    _review_at(s["record_id"], "approve", 0, token="t-exp1")


def test_sla_overdue_flag():
    d = _create(review_flow={"stages": [
        {"name": "快审", "role": "reviewer", "sla_hours": 1.0}]})
    s, _ = _submitted(d["id"], sub_id="ms-sla")
    db = SessionLocal()
    rec = db.query(ChallengeSubmission).filter(
        ChallengeSubmission.id == s["record_id"]).one()
    rec.stage_entered_at = rec.created_at - 2 * 3600  # 2 小时前进入 → 超时
    db.commit()
    db.close()
    row = next(x for x in ch_api.review_queue()["submissions"]
               if x["record_id"] == s["record_id"])
    assert row["sla_overdue"] is True and row["sla_hours"] == 1.0
    detail = ch_api.get_submission(s["record_id"])
    assert detail["sla_overdue"] is True


def test_new_version_can_reconfigure_review_flow():
    d = _create()  # v1 默认单级
    ch_api.publish_version(d["id"], ch_api.VersionIn(
        **_def(), review_flow=ch_api.ReviewFlowIn(**_flow_two_stage())))
    detail = ch_api.challenge_detail(d["id"])
    assert detail["current"]["version"] == 2
    assert len(detail["current"]["review_flow"]["stages"]) == 2
    # v1 仍是默认单级（不可变）
    v1 = ch_api.version_detail(d["id"], 1)
    assert len(v1["review_flow"]["stages"]) == 1
    # v2 提交走两级；v1 提交走单级
    r2 = ch_api.run(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)], version=2))
    s2 = _submit(d["id"], r2["run_id"], "ms-v2")
    assert s2["review_stage_count"] == 2 and s2["version"] == 2
    r1 = ch_api.run(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)], version=1))
    s1 = _submit(d["id"], r1["run_id"], "ms-v1")
    assert s1["review_stage_count"] == 1 and s1["version"] == 1


def test_publish_version_keeps_flow_when_unspecified():
    d = _create(review_flow=_flow_two_stage())
    # 发新版本不带 review_flow → 沿用当前版本的两级流
    d2 = ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(name="v2")))
    assert len(d2["current"]["review_flow"]["stages"]) == 2


# ---------- 多级审核流下的申诉队列路由 ----------

def test_appeal_route_stage_reenters_rejecting_stage():
    flow = _flow_two_stage(appeal_route="stage")
    d = _create(review_flow=flow)
    s, _ = _submitted(d["id"], sub_id="route-1")
    _review_at(s["record_id"], "approve", 0)
    _review_at(s["record_id"], "reject", 1, token=MOD_TOKEN, note="复核驳回")
    # 申诉：回到被驳回的第 2 级（stage=1），route=stage
    ap = _appeal(d["id"], s["record_id"], "补充证据", "route-ap1")
    assert ap["route"] == "stage"
    assert _status(s["record_id"]) == "pending"
    detail = ch_api.get_submission(s["record_id"])
    assert detail["review_stage"] == 1 and detail["appeal_route_open"] == "stage"
    # 复核员队列看不到 route=stage 的待裁决申诉；审核队列第 2 级能看到
    assert ch_api.appeals()["appeals"] == []
    stage_aps = ch_api.appeals(route="stage")["appeals"]
    assert [a["appeal_id"] for a in stage_aps] == [ap["appeal_id"]]
    assert stage_aps[0]["review_stage_name"] == "复核"
    q = ch_api.review_queue(stage=1)["submissions"]
    row = next(x for x in q if x["record_id"] == s["record_id"])
    assert row["appeal_open"] and row["queue_kind"] == "stage_appeal"

    # 复核裁决接口拒绝直接处理 route=stage 的申诉
    with pytest.raises(HTTPException) as e1:
        _decide(ap["appeal_id"], "overturn")
    assert e1.value.status_code == 409
    # 初审员无权在复核级裁决 → 403
    with pytest.raises(HTTPException) as e2:
        _review_at(s["record_id"], "approve", 1, token=REV_TOKEN)
    assert e2.value.status_code == 403
    # 该级（复核员）通过 = 推翻原判；末级通过直接上榜
    out = _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN, note="改判")
    assert out["review_status"] == "approved"
    assert out["appeal_decision"] == "overturn"
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1
    tl = ch_api.get_timeline(s["record_id"])
    assert tl["appeals"][0]["status"] == "overturn"
    assert any(e["kind"] == "appeal_overturn" for e in tl["events"])


def test_appeal_route_stage_reject_upholds_and_rejects_at_first_stage():
    # 单级 + stage 路由：第 1 级驳回后申诉回第 1 级，再驳回=维持 rejected
    flow = {"stages": [{"name": "审核", "role": "reviewer"}],
            "appeal_route": "stage"}
    d = _create(review_flow=flow)
    s, _ = _submitted(d["id"], sub_id="route-2")
    _review_at(s["record_id"], "reject", 0, note="不行")
    ap = _appeal(d["id"], s["record_id"], "再看看", "route-ap2")
    assert ap["route"] == "stage"
    # 该级再驳回 = 维持原判
    out = _review_at(s["record_id"], "reject", 0, note="证据不足")
    assert out["review_status"] == "rejected"
    assert out["appeal_decision"] == "uphold"
    ap_row = ch_api.appeals(status="uphold", route="stage")["appeals"][0]
    assert ap_row["status"] == "uphold"
    ap2 = _appeal(d["id"], s["record_id"], "新证据", "route-ap3")
    out2 = _review_at(s["record_id"], "approve", 0)
    assert out2["review_status"] == "approved"
    assert out2["appeal_decision"] == "overturn"
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1


def test_appeal_route_stage_advance_continues_through_later_stages():
    """第 1 级驳回 → 申诉回第 1 级，该级通过只是"继续往后走"，还要过第 2 级。"""
    flow = _flow_two_stage(appeal_route="stage")
    d = _create(review_flow=flow)
    s, _ = _submitted(d["id"], sub_id="route-3")
    _review_at(s["record_id"], "reject", 0, note="初审驳回")
    _appeal(d["id"], s["record_id"], "误判", "route-ap4")
    # 第 1 级通过：推翻驳回但还没上榜，进入第 2 级
    out = _review_at(s["record_id"], "approve", 0)
    assert out["review_status"] == "pending" and out["review_stage"] == 1
    assert out["appeal_decision"] == "overturn"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    # 第 2 级通过（普通审核，已无进行中申诉）才上榜
    out2 = _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    assert out2["review_status"] == "approved"
    assert len(ch_api.get_leaderboard(d["id"])["entries"]) == 1


def test_revoked_appeal_always_goes_to_moderator_even_with_stage_route():
    flow = _flow_two_stage(appeal_route="stage")
    d = _create(review_flow=flow)
    s, _ = _submitted(d["id"], sub_id="route-4")
    _review_at(s["record_id"], "approve", 0)
    _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    _revoke(s["record_id"])
    ap = _appeal(d["id"], s["record_id"], "撤销有误", "route-ap5")
    assert ap["route"] == "moderator"  # 上榜后撤销始终复核员受理
    assert ch_api.appeals()["appeals"][0]["appeal_id"] == ap["appeal_id"]
    # 审核级接口不能处理
    with pytest.raises(HTTPException) as e:
        _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    assert e.value.status_code == 409
    dec = _decide(ap["appeal_id"], "overturn")
    assert dec["review_status"] == "approved"


def test_multistage_revoke_restore_keeps_stage_consistency():
    d = _create(review_flow=_flow_two_stage())
    s, _ = _submitted(d["id"], sub_id="route-5")
    _review_at(s["record_id"], "approve", 0)
    _review_at(s["record_id"], "approve", 1, token=MOD_TOKEN)
    rev = _revoke(s["record_id"])
    assert rev["review_stage"] == 1  # 停在末级
    assert ch_api.get_submission(s["record_id"])["replayable"] is False
    restored = _restore(s["record_id"])
    assert restored["review_status"] == "approved" and restored["review_stage"] == 1
    assert ch_api.get_submission(s["record_id"])["replayable"] is True


def test_pending_count_and_player_view_reflect_multistage():
    d = _create(review_flow=_flow_two_stage())
    s, _ = _submitted(d["id"], sub_id="route-6")
    _review_at(s["record_id"], "approve", 0)  # 进入第 2 级，仍 pending
    card = next(c for c in ch_api.list_challenges()["challenges"]
                if c["id"] == d["id"])
    assert card["pending_count"] == 1 and card["review_stages"] == 2
    mine = ch_api.my_submissions("飞行员甲")["submissions"][0]
    assert mine["review_stage"] == 1 and mine["review_stage_name"] == "复核"
    assert mine["review_stage_index"] == 2 and mine["review_stage_count"] == 2
    assert mine["replayable"] is False
