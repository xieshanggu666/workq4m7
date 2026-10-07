/* 视图：社区航线挑战（挑战列表 / 发布版本化关卡 / 审核队列） */
window.ChallengeSelect = {
  name: "ChallengeSelect",
  props: ["challenges", "bodies"],
  data() {
    return {
      mode: "list",          // list | publish | review
      reviewTab: "queue",    // queue=审核队列 appeals=申诉复核 archive=终审管理 mine=我的成绩
      form: null,
      publishErr: "",
      publishing: false,
      queue: [],
      queueLoading: false,
      appeals: [],
      archive: [],
      archiveStatus: "approved",
      mine: [],
      minePlayer: Store.get("pilot") || "",
      timeline: null,
      timelineLoading: false,
      moderatorToken: Store.get("modToken") || "local-moderator",
      reviewerToken: Store.get("revToken") || "",
      actionErr: "",
      stageFilter: "",        // 审核队列按级过滤（""=全部）
    };
  },
  computed: {
    bodyById() {
      const m = {};
      for (const b of this.bodies || []) m[b.id] = b;
      return m;
    },
    planetOptions() {
      return (this.bodies || []).filter(b => b.id !== "sun");
    },
    pendingTotal() {
      return (this.challenges || []).reduce((s, c) => s + (c.pending_count || 0), 0);
    },
  },
  methods: {
    kmps(v) { return (v * 1731.5).toFixed(1); },
    starOf(c) {
      const best = c.best_current || c.best;
      return best ? best.stars : 0;
    },
    fmtDate(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    },
    fmtDateTime(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${this.fmtDate(ts)} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    statusLabel(s) {
      return { pending: "待审核", approved: "已上榜", rejected: "已驳回",
               revoked: "已撤销" }[s] || s;
    },
    statusClass(s) {
      return { pending: "tag-pending", approved: "tag-ok",
               rejected: "tag-bad", revoked: "tag-warn" }[s] || "";
    },
    roleLabel(r) {
      return r === "moderator" ? "复核员" : "初审员";
    },
    stageBadge(r) {
      if (!r || r.review_stage_count == null) return "";
      return `第 ${r.review_stage_index || ((r.review_stage || 0) + 1)}/${r.review_stage_count} 级 · ${r.review_stage_name || ""}`;
    },
    fmtSla(ts) {
      if (!ts) return "";
      return this.fmtDateTime(ts);
    },
    eventLabel(k) {
      return {
        submit: "玩家提交", review_approve: "初审通过", review_reject: "初审驳回",
        appeal: "玩家申诉", appeal_uphold: "复核维持原判",
        appeal_overturn: "复核推翻·改判通过", revoke: "复核撤销上榜",
        restore: "复核恢复上榜", legacy: "历史已审核（补登）",
      }[k] || k;
    },
    eventDetail(e) {
      const d = e.detail || {};
      if (e.kind === "submit") return `★${d.stars} · 服务端结算`;
      if (e.kind === "appeal")
        return `第 ${d.round} 轮（原判 ${this.statusLabel(d.from_status)}${
          d.route === "stage" ? " · 回到审核级重审" : ""}）：${d.reason || ""}`;
      if (e.kind === "appeal_uphold" || e.kind === "appeal_overturn")
        return `第 ${d.round} 轮${d.stage_name ? " · " + d.stage_name : ""}${d.note ? "：" + d.note : ""}`;
      if ((e.kind === "review_approve" || e.kind === "review_reject")
          && d.stage_name) {
        const verb = e.kind === "review_approve" ? "通过" : "驳回";
        return `${d.stage_name}（第 ${(d.stage || 0) + 1} 级）${verb}${d.final ? " · 终审上榜" : ""}${d.note ? "：" + d.note : ""}`;
      }
      return d.note || "";
    },
    enter(c) { if (c.unlocked) this.$emit("enter", c); },

    // ---------- 发布（新挑战 / 新版本） ----------
    blankForm() {
      return {
        mode: "create", targetId: null, targetTitle: "",
        title: "", author: Store.get("designer") || "",
        name: "", brief: "", hint: "",
        budgetKm: 8.0, tMax: 1200,
        milestones: [{ kind: "proximity", planet_id: "mars", dist: 0.18, r: 4.2, name: "" }],
        unlock: { type: "none", level_id: 1, challenge_id: null, value: 5 },
        // 多级审核流：null=创建时用默认单级流（发版沿用当前版本）；编辑时展开为具体配置
        flowEnabled: false,
        stages: [this.blankStage()],
        appealRoute: "moderator",
      };
    },
    blankStage() {
      return { name: "", role: "reviewer", reviewersText: "", slaHours: null };
    },
    addStage() {
      if (this.form.stages.length >= 5) return;
      this.form.stages.push(this.blankStage());
    },
    removeStage(i) {
      if (this.form.stages.length <= 1) return;
      this.form.stages.splice(i, 1);
    },
    openPublish(ch) {
      const f = this.blankForm();
      this.publishErr = "";
      if (!ch) { this.form = f; this.mode = "publish"; return; }
      // 发布新版本：拉取当前版本定义预填，在其上修改
      f.mode = "version"; f.targetId = ch.id; f.targetTitle = ch.title;
      this.form = f; this.mode = "publish";
      API.challenge(ch.id).then(d => {
        const v = d.current;
        f.name = "";  // 版本名留空，由设计者重新命名
        f.brief = v.brief; f.hint = v.hint;
        f.budgetKm = +(v.budget_dv * 1731.5).toFixed(1);
        f.tMax = Math.round(v.t_max);
        f.milestones = v.milestones.map(m => ({
          kind: m.kind, name: m.name,
          planet_id: m.planet_id || "mars",
          dist: m.dist || 0.18, r: m.r || 4.2,
        }));
        if (d.unlock_rule) f.unlock = { level_id: 1, value: 5, challenge_id: null, ...d.unlock_rule };
        // 预填当前版本的审核流：自定义多级流才展开编辑，默认单级保持折叠（沿用）
        const rf = (d.current && d.current.review_flow) || {};
        if (rf.custom) {
          f.flowEnabled = true;
          f.appealRoute = rf.appeal_route || "moderator";
          f.stages = (rf.stages || []).map(st => ({
            name: st.name, role: st.role,
            reviewersText: (st.reviewers || []).join("、"),
            slaHours: st.sla_hours == null ? null : st.sla_hours,
          }));
        }
      }).catch(e => { this.publishErr = e.message; });
    },
    addMs() {
      this.form.milestones.push({ kind: "radius", planet_id: "mars", dist: 0.18, r: 4.2, name: "" });
    },
    removeMs(i) { this.form.milestones.splice(i, 1); },
    needsPlanet(kind) {
      return ["proximity", "assist_capture", "assist_then_radius"].includes(kind);
    },
    needsR(kind) {
      return ["radius", "assist_then_radius", "escape"].includes(kind);
    },
    msKindLabel(k) {
      return { proximity: "近距飞掠", radius: "半径达标", assist_capture: "引力捕获",
               assist_then_radius: "弹弓后半径", escape: "逃逸边界" }[k] || k;
    },
    async submitPublish() {
      const f = this.form;
      const def = {
        name: f.name, brief: f.brief, hint: f.hint,
        budget_dv: f.budgetKm / 1731.5,
        t_max: f.tMax,
        milestones: f.milestones.map(m => {
          const it = { kind: m.kind, name: m.name || this.msKindLabel(m.kind) };
          if (this.needsPlanet(m.kind)) it.planet_id = m.planet_id;
          if (m.kind === "proximity") it.dist = m.dist;
          if (this.needsR(m.kind)) it.r = m.r;
          return it;
        }),
      };
      const rule = f.unlock.type === "none" ? { type: "none" } : { ...f.unlock };
      // 审核流：仅在设计者显式启用多级配置时随版本提交；否则由后端默认/沿用
      let reviewFlow = undefined;
      if (f.flowEnabled) {
        reviewFlow = {
          appeal_route: f.appealRoute,
          stages: f.stages.map((s, i) => ({
            name: (s.name || "").trim() || `第 ${i + 1} 级`,
            role: s.role,
            reviewers: (s.reviewersText || "")
              .split(/[、,，\s]+/).map(x => x.trim()).filter(Boolean),
            sla_hours: (s.slaHours === null || s.slaHours === "" || isNaN(+s.slaHours))
              ? null : +s.slaHours,
          })),
        };
      }
      this.publishing = true;
      this.publishErr = "";
      try {
        if (f.mode === "create") {
          const payload = { title: f.title, author: f.author, ...def, unlock_rule: rule };
          if (reviewFlow) payload.review_flow = reviewFlow;
          await API.createChallenge(payload);
        } else {
          const payload = { ...def, unlock_rule: rule };
          if (reviewFlow) payload.review_flow = reviewFlow;
          await API.publishVersion(f.targetId, payload);
        }
        Store.set("designer", f.author);
        this.mode = "list";
        this.$emit("refresh");
      } catch (e) {
        this.publishErr = e.message;
      } finally {
        this.publishing = false;
      }
    },

    // ---------- 审核 / 申诉 / 复核 ----------
    async openReview(tab = "queue") {
      this.mode = "review";
      this.reviewTab = tab;
      this.actionErr = "";
      await this.loadReviewTab(tab);
    },
    async loadReviewTab(tab) {
      this.reviewTab = tab;
      this.queueLoading = true;
      this.actionErr = "";
      try {
        if (tab === "queue") {
          await this.loadQueue();
        } else if (tab === "appeals") {
          this.appeals = (await API.appealsQueue("pending")).appeals || [];
        } else if (tab === "archive") {
          this.archive = (await API.reviewQueueStatus(this.archiveStatus)).submissions || [];
        } else if (tab === "mine") {
          await this.loadMine();
        }
      } catch (e) {
        this.actionErr = e.message;
        this.queue = []; this.appeals = []; this.archive = [];
      } finally {
        this.queueLoading = false;
      }
    },
    async loadQueue() {
      this.queueLoading = true;
      try {
        const stage = this.stageFilter === "" ? null : Number(this.stageFilter);
        this.queue = (await API.reviewQueue(stage, this.reviewerToken || null)).submissions || [];
      } catch (e) {
        this.actionErr = e.message;
        this.queue = [];
      } finally {
        this.queueLoading = false;
      }
    },
    saveRevToken() { Store.set("revToken", this.reviewerToken); this.loadQueue(); },
    async switchArchive(status) {
      this.archiveStatus = status;
      this.queueLoading = true;
      try {
        this.archive = (await API.reviewQueueStatus(status)).submissions || [];
      } catch (e) {
        this.actionErr = e.message;
      } finally {
        this.queueLoading = false;
      }
    },
    async loadMine() {
      this.queueLoading = true;
      try {
        this.mine = this.minePlayer
          ? ((await API.mySubmissions(this.minePlayer)).submissions || []) : [];
      } catch (e) {
        this.actionErr = e.message;
        this.mine = [];
      } finally {
        this.queueLoading = false;
      }
    },
    async review(rec, action) {
      try {
        await API.reviewSubmission(rec.record_id, action, rec._note || "",
                                   this.reviewerToken || null, rec.review_stage);
        await this.loadQueue();
        this.$emit("refresh");
      } catch (e) {
        this.actionErr = "审核失败：" + e.message;
      }
    },
    async decide(ap, decision) {
      try {
        await API.decideAppeal(ap.appeal_id, decision, ap._note || "",
                               this.moderatorToken);
        await this.loadReviewTab("appeals");
        this.$emit("refresh");
      } catch (e) {
        this.actionErr = "复核裁决失败：" + e.message;
      }
    },
    async revoke(rec) {
      const note = rec._note || "";
      if (!window.confirm(`确认撤销该上榜成绩？将立即移出排行榜、关闭回放，并级联回收它解锁的挑战。`)) return;
      try {
        const out = await API.revokeSubmission(rec.record_id, note,
                                               this.moderatorToken);
        await this.loadReviewTab(this.reviewTab);
        this.$emit("refresh");
        this._showRollback(out);
      } catch (e) {
        this.actionErr = "撤销失败：" + e.message;
      }
    },
    async restore(rec) {
      try {
        await API.restoreSubmission(rec.record_id, rec._note || "",
                                    this.moderatorToken);
        await this.loadReviewTab(this.reviewTab);
        this.$emit("refresh");
      } catch (e) {
        this.actionErr = "恢复失败：" + e.message;
      }
    },
    _showRollback(out) {
      const ch = (out.effects && out.effects.unlock_changes) || [];
      const locked = ch.filter(c => !c.unlocked);
      if (locked.length) {
        this.actionErr = "已撤销；级联回收解锁：" +
          locked.map(c => `「${c.title}」`).join("、");
      }
    },
    async appeal(rec) {
      const reason = window.prompt(
        `对 #${rec.record_id}「${rec.challenge_title}」的${this.statusLabel(rec.review_status)}结果发起申诉（第 ${rec.appeal_rounds + 1} 轮，最多 2 轮）：\n请说明申诉理由，复核员会重新核查服务端结算档案。`,
        rec._appealReason || "");
      if (reason === null) return;
      if (!reason.trim()) { this.actionErr = "申诉理由不能为空"; return; }
      const appealId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : `ap-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      try {
        await API.appealSubmission(rec.record_id, this.minePlayer,
                                   reason.trim(), appealId);
        await this.loadReviewTab("mine");
        this.$emit("refresh");
      } catch (e) {
        this.actionErr = "申诉失败：" + e.message;
      }
    },
    async showTimeline(recordId) {
      this.timelineLoading = true;
      this.timeline = null;
      try {
        this.timeline = await API.submissionTimeline(recordId);
      } catch (e) {
        this.actionErr = "加载审核链路失败：" + e.message;
      } finally {
        this.timelineLoading = false;
      }
    },
  },
  template: `
  <div class="screen levels-screen">
    <header class="topbar">
      <div class="logo">
        <div class="logo-mark">🌐</div>
        <div>
          <h1>社区航线挑战</h1>
          <p>设计者发布版本化关卡 · 飞行记录审核后上榜</p>
        </div>
      </div>
      <div class="topbar-right">
        <button class="btn ghost" @click="$emit('back')">← 返回关卡</button>
        <button class="btn ghost" @click="openReview">
          🗂 审核队列 <span v-if="pendingTotal" class="badge">{{ pendingTotal }}</span>
        </button>
        <button class="btn primary" @click="openPublish(null)">✎ 发布挑战</button>
      </div>
    </header>

    <!-- 挑战列表 -->
    <div v-if="mode === 'list'" class="level-grid">
      <div v-for="c in challenges" :key="c.id"
           class="level-card" :class="{ locked: !c.unlocked }"
           @click="enter(c)">
        <div class="level-num">挑战 #{{ c.id }} · v{{ c.current_version }} · {{ c.version_count }} 个版本</div>
        <h2>{{ c.title }}</h2>
        <p class="brief">{{ c.brief || '（设计者没有留下简介）' }}</p>
        <div class="best-meta">
          设计 {{ c.author }} · 预算 {{ kmps(c.budget_dv) }} km/s · {{ c.milestone_count }} 个里程碑
          · 🗂 {{ c.review_stages || 1 }} 级审核
          <span v-if="c.pending_count" class="pending-tag">待审 {{ c.pending_count }}</span>
        </div>
        <div class="best-meta" v-if="c.best_current">
          当前 v{{ c.current_version }} 最佳 {{ kmps(c.best_current.fuel_used) }} km/s · {{ Math.round(c.best_current.elapsed_days) }} 天
          <span class="ver-tag">v{{ c.best_current.version }}</span>
          <span class="replayable-tag">已上榜</span>
        </div>
        <div class="best-meta" v-else-if="c.best">
          旧版最佳 {{ kmps(c.best.fuel_used) }} km/s · {{ Math.round(c.best.elapsed_days) }} 天
          <span class="ver-tag">v{{ c.best.version }}</span>
          <span class="replayable-tag">旧版可回放</span>
        </div>
        <div class="level-foot">
          <span class="stars">
            <template v-for="i in 3" :key="i">
              <span :class="i <= starOf(c) ? 'lit' : 'dim'">★</span>
            </template>
          </span>
          <span v-if="!c.unlocked" class="lock-tag">🔒 {{ c.unlock_desc }}</span>
          <span v-else class="go-btn">进入挑战 →</span>
        </div>
        <div class="card-ops">
          <button class="btn mini" @click.stop="openPublish(c)">＋ 发布新版本</button>
        </div>
      </div>
      <p v-if="!challenges.length" class="plan-empty empty-tip">
        还没有社区挑战。点击右上角「发布挑战」，设计第一条社区航线！
      </p>
    </div>

    <!-- 发布表单 -->
    <div v-if="mode === 'publish' && form" class="form-wrap">
      <div class="form-panel">
        <h2>{{ form.mode === 'create' ? '发布新挑战' : '发布新版本 · ' + form.targetTitle }}</h2>
        <p class="form-sub">每次发布生成不可变版本；成绩按版本结算排行榜，旧版本仍可回放。</p>
        <div class="form-grid">
          <template v-if="form.mode === 'create'">
            <label>挑战标题
              <input v-model.trim="form.title" maxlength="40" placeholder="例如：木星捷径">
            </label>
            <label>设计者署名
              <input v-model.trim="form.author" maxlength="24" placeholder="匿名设计者">
            </label>
          </template>
          <label>版本名（可选）
            <input v-model.trim="form.name" maxlength="40" placeholder="例如：加严预算版">
          </label>
          <label>燃料预算（km/s）
            <input type="number" v-model.number="form.budgetKm" min="0.3" max="90" step="0.1">
          </label>
          <label>时间限制（天）
            <input type="number" v-model.number="form.tMax" min="30" max="20000" step="10">
          </label>
          <label>解锁条件
            <select v-model="form.unlock.type">
              <option value="none">无（直接开放）</option>
              <option value="builtin_level">通关内置关卡</option>
              <option value="challenge">通关指定挑战</option>
              <option value="stars_total">累计星数达标</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'builtin_level'">前置内置关卡
            <select v-model.number="form.unlock.level_id">
              <option v-for="i in 5" :key="i" :value="i">第 {{ i }} 关</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'challenge'">前置挑战
            <select v-model.number="form.unlock.challenge_id">
              <option v-for="c in challenges" :key="c.id" :value="c.id"
                      :disabled="c.id === form.targetId">#{{ c.id }} {{ c.title }}</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'stars_total'">所需累计星数
            <input type="number" v-model.number="form.unlock.value" min="1" max="99">
          </label>
          <label class="span2">关卡简介
            <textarea v-model.trim="form.brief" rows="2" maxlength="500"
                      placeholder="这条航线的故事与目标…"></textarea>
          </label>
          <label class="span2">策略提示
            <textarea v-model.trim="form.hint" rows="2" maxlength="500"
                      placeholder="给玩家的一点提示（可选）…"></textarea>
          </label>
        </div>

        <h3 class="form-h3">里程碑（{{ form.milestones.length }}/5）</h3>
        <div class="ms-list">
          <div v-for="(m, i) in form.milestones" :key="i" class="ms-row">
            <select v-model="m.kind">
              <option value="proximity">近距飞掠</option>
              <option value="radius">半径达标</option>
              <option value="assist_capture">引力捕获</option>
              <option value="assist_then_radius">弹弓后半径</option>
              <option value="escape">逃逸边界</option>
            </select>
            <select v-if="needsPlanet(m.kind)" v-model="m.planet_id">
              <option v-for="p in planetOptions" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <input v-if="m.kind === 'proximity'" type="number" v-model.number="m.dist"
                   step="0.01" min="0.001" max="5" title="飞掠距离 AU" class="num">
            <input v-if="needsR(m.kind)" type="number" v-model.number="m.r"
                   step="0.1" min="0.2" max="100" title="半径 AU" class="num">
            <input class="ms-name" v-model.trim="m.name" maxlength="40"
                   :placeholder="msKindLabel(m.kind)">
            <button class="icon-btn danger" @click="removeMs(i)"
                    :disabled="form.milestones.length <= 1">✕</button>
          </div>
        </div>
        <button class="btn chip" @click="addMs" :disabled="form.milestones.length >= 5">
          + 添加里程碑
        </button>

        <h3 class="form-h3">
          <label class="flow-switch">
            <input type="checkbox" v-model="form.flowEnabled">
            自定义多级审核流（{{ form.flowEnabled ? form.stages.length + ' 级' : '默认单级初审' }}）
          </label>
        </h3>
        <div v-if="form.flowEnabled" class="flow-editor">
          <p class="form-sub">
            提交后逐级审核，全部通过才上榜；每级可指定角色、审核人（留空=该角色全员）与审核时限。
          </p>
          <div v-for="(st, i) in form.stages" :key="i" class="stage-row">
            <span class="stage-no">第 {{ i + 1 }} 级</span>
            <input v-model.trim="st.name" maxlength="24" :placeholder="'级名称（如 初审/终审）'"
                   class="stage-name">
            <select v-model="st.role">
              <option value="reviewer">初审员</option>
              <option value="moderator">复核员</option>
            </select>
            <input v-model.trim="st.reviewersText" maxlength="120"
                   placeholder="指定审核人署名（、分隔，留空=该角色全员）" class="stage-reviewers">
            <input type="number" v-model.number="st.slaHours" min="0.5" max="720" step="0.5"
                   placeholder="时限(小时，可空)" class="stage-sla">
            <button class="icon-btn danger" @click="removeStage(i)"
                    :disabled="form.stages.length <= 1">✕</button>
          </div>
          <button class="btn chip" @click="addStage" :disabled="form.stages.length >= 5">
            + 添加审核级（最多 5 级）
          </button>
          <label class="flow-route">申诉去向
            <select v-model="form.appealRoute">
              <option value="moderator">复核员裁决（默认）</option>
              <option value="stage">回到驳回级，由该级审核人重审</option>
            </select>
          </label>
        </div>

        <p v-if="publishErr" class="form-err">⚠ {{ publishErr }}</p>
        <div class="form-btns">
          <button class="btn ghost" @click="mode = 'list'">取消</button>
          <button class="btn primary" :disabled="publishing" @click="submitPublish">
            {{ publishing ? '发布中…' : (form.mode === 'create' ? '发布挑战' : '发布新版本') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 审核 / 申诉 / 复核工作台 -->
    <div v-if="mode === 'review'" class="form-wrap">
      <div class="form-panel review-panel">
        <h2>审核工作台
          <span class="plan-count">多级审核 · 玩家申诉 · 复核撤销/恢复 · 全链路可追溯</span>
        </h2>
        <div class="review-tabs">
          <button :class="{ active: reviewTab === 'queue' }"
                  @click="loadReviewTab('queue')">🗂 审核队列</button>
          <button :class="{ active: reviewTab === 'appeals' }"
                  @click="loadReviewTab('appeals')">⚖ 申诉复核
            <span v-if="appeals.length" class="badge">{{ appeals.length }}</span></button>
          <button :class="{ active: reviewTab === 'archive' }"
                  @click="loadReviewTab('archive')">📜 终审管理</button>
          <button :class="{ active: reviewTab === 'mine' }"
                  @click="loadReviewTab('mine')">🧑‍✈️ 我的成绩/申诉</button>
        </div>
        <p v-if="actionErr" class="form-err">⚠ {{ actionErr }}</p>

        <!-- 审核队列：多级审核流，按版本配置把成绩送到当前审核级 -->
        <template v-if="reviewTab === 'queue'">
          <div class="archive-bar">
            <label class="mod-token">审核令牌
              <input v-model.trim="reviewerToken"
                     @change="saveRevToken()"
                     placeholder="留空=本机初审员；复核级填复核员令牌">
            </label>
            <label class="mod-token">按级过滤
              <select v-model="stageFilter" @change="loadQueue()">
                <option value="">全部审核级</option>
                <option v-for="n in 5" :key="n" :value="n - 1">第 {{ n }} 级</option>
              </select>
            </label>
          </div>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">没有待审核的飞行记录。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row"
               :class="{ 'appeal-row': r.appeal_open, 'sla-row': r.sla_overdue }">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span class="stage-tag">{{ stageBadge(r) }} · {{ roleLabel(r.review_stage_role) }}</span>
              <span v-if="r.review_stage_reviewers && r.review_stage_reviewers.length"
                    class="stage-named">指定：{{ r.review_stage_reviewers.join('、') }}</span>
              <span v-if="r.sla_overdue" class="sla-tag">⏰ 已超时限（{{ r.sla_hours }}h）</span>
              <span v-else-if="r.sla_hours" class="sla-ok">时限 {{ r.sla_hours }}h</span>
              <span v-if="r.appeal_open && r.appeal_route_open === 'stage'"
                    class="appeal-tag">第 {{ r.appeal_round }} 轮申诉·回到本级重审</span>
              <span v-else-if="r.appeal_open" class="appeal-tag">
                第 {{ r.appeal_round }} 轮申诉待复核员裁决</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天 · {{ fmtDate(r.created_at) }}
              </div>
              <div v-if="r.appeal_open && r.appeal_route_open === 'stage'"
                   class="queue-meta appeal-reason">
                申诉理由：{{ r.appeal_reason }}（本级通过=推翻并继续流转，驳回=维持原判）
              </div>
              <div v-else-if="r.appeal_open" class="queue-meta appeal-reason">
                申诉理由：{{ r.appeal_reason }}（请在「申诉复核」页裁决，审核队列不可处理）
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._note" maxlength="200"
                   placeholder="审核备注（可选）"
                   :disabled="r.queue_kind === 'moderator_appeal' || r.can_review === false">
            <button class="btn mini approve"
                    :disabled="r.queue_kind === 'moderator_appeal' || r.can_review === false"
                    @click="review(r, 'approve')">✓ 通过</button>
            <button class="btn mini reject"
                    :disabled="r.queue_kind === 'moderator_appeal' || r.can_review === false"
                    @click="review(r, 'reject')">✕ 驳回</button>
            <button class="btn mini ghost" @click="showTimeline(r.record_id)">链路</button>
          </div>
        </template>

        <!-- 申诉复核（仅复核员） -->
        <template v-if="reviewTab === 'appeals'">
          <label class="mod-token">复核员令牌
            <input v-model.trim="moderatorToken"
                   @change="Store.set('modToken', moderatorToken)"
                   placeholder="local-moderator（本地单机默认）">
          </label>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!appeals.length" class="plan-empty">没有待复核的申诉。</p>
          <div v-for="a in appeals" :key="a.appeal_id" class="queue-row appeal-row">
            <div class="queue-main">
              <b>#{{ a.challenge_id }} {{ a.challenge_title }}</b>
              <span class="ver-tag">v{{ a.version }}</span>
              <span class="appeal-tag">第 {{ a.round }} 轮申诉 · 原判 {{ statusLabel(a.from_status) }}</span>
              <div class="queue-meta">
                {{ a.player }} · ★{{ a.stars }} · {{ kmps(a.fuel_used) }} km/s
                · {{ Math.round(a.elapsed_days) }} 天
              </div>
              <div class="queue-meta appeal-reason">申诉理由：{{ a.reason }}</div>
            </div>
            <input class="queue-note" v-model.trim="a._note" maxlength="200"
                   placeholder="裁决备注（可选）">
            <button class="btn mini approve" @click="decide(a, 'overturn')">
              ↺ 推翻·改判通过</button>
            <button class="btn mini reject" @click="decide(a, 'uphold')">
              ⊘ 维持原判</button>
            <button class="btn mini ghost" @click="showTimeline(a.record_id)">链路</button>
          </div>
        </template>

        <!-- 终审管理：撤销/恢复上榜成绩 -->
        <template v-if="reviewTab === 'archive'">
          <div class="archive-bar">
            <label class="mod-token">复核员令牌
              <input v-model.trim="moderatorToken"
                     @change="Store.set('modToken', moderatorToken)"
                     placeholder="local-moderator（本地单机默认）">
            </label>
            <div class="status-switch">
              <button :class="{ active: archiveStatus === 'approved' }"
                      @click="switchArchive('approved')">已上榜</button>
              <button :class="{ active: archiveStatus === 'revoked' }"
                      @click="switchArchive('revoked')">已撤销</button>
              <button :class="{ active: archiveStatus === 'rejected' }"
                      @click="switchArchive('rejected')">已驳回</button>
            </div>
          </div>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!archive.length" class="plan-empty">该状态下暂无成绩。</p>
          <div v-for="r in archive" :key="r.record_id" class="queue-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span :class="'status-tag ' + statusClass(r.review_status)">
                {{ statusLabel(r.review_status) }}</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天
                <template v-if="r.reviewed_by"> · 审核 {{ r.reviewed_by }}</template>
                <template v-if="r.review_note"> · 备注 {{ r.review_note }}</template>
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._note" maxlength="200"
                   placeholder="复核备注（可选）">
            <button v-if="r.review_status === 'approved'"
                    class="btn mini reject" @click="revoke(r)">⤼ 撤销上榜</button>
            <button v-if="r.review_status === 'revoked'"
                    class="btn mini approve" @click="restore(r)">↩ 恢复上榜</button>
            <button class="btn mini ghost" @click="showTimeline(r.record_id)">链路</button>
          </div>
        </template>

        <!-- 我的成绩 / 发起申诉 -->
        <template v-if="reviewTab === 'mine'">
          <label class="mod-token">飞行员署名
            <input v-model.trim="minePlayer" maxlength="24"
                   @change="loadMine()" placeholder="匿名飞行员">
            <button class="btn mini" @click="Store.set('pilot', minePlayer); loadMine()">
              查询</button>
          </label>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!mine.length" class="plan-empty">
            还没有提交记录。先进入挑战发射一次吧！
          </p>
          <div v-for="r in mine" :key="r.record_id" class="queue-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span :class="'status-tag ' + statusClass(r.review_status)">
                {{ statusLabel(r.review_status) }}</span>
              <span v-if="r.review_status === 'pending'" class="stage-tag">
                {{ stageBadge(r) }}</span>
              <span v-if="r.appeal_open" class="appeal-tag">
                第 {{ r.appeal_rounds }} 轮申诉{{ r.appeal_route === 'stage' ? '·回到审核级' : '复核中' }}</span>
              <div class="queue-meta">
                ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天 · {{ fmtDate(r.created_at) }}
              </div>
              <div v-if="r.review_note" class="queue-meta">审核备注：{{ r.review_note }}</div>
            </div>
            <button class="btn mini approve" :disabled="!r.appeal_available"
                    @click="appeal(r)">
              {{ r.appeal_rounds >= 2 ? '申诉轮次已用尽'
                 : (r.appeal_open ? '申诉复核中' : '⚖ 发起申诉（剩 ' + (2 - r.appeal_rounds) + ' 轮）') }}
            </button>
            <button class="btn mini ghost" @click="showTimeline(r.record_id)">链路</button>
          </div>
        </template>

        <div class="form-btns">
          <button class="btn ghost" @click="mode = 'list'">← 返回列表</button>
        </div>
      </div>
    </div>

    <!-- 审核链路时间线 -->
    <div v-if="timeline" class="modal-mask" @click.self="timeline = null">
      <div class="modal timeline-modal">
        <h2>🔗 审核链路 · 成绩 #{{ timeline.record_id }}</h2>
        <p class="modal-reason">
          #{{ timeline.challenge_id }} {{ timeline.challenge_title }} · v{{ timeline.version }}
          · {{ timeline.player }} · ★{{ timeline.stars }}
          · {{ kmps(timeline.fuel_used) }} km/s
          <span :class="'status-tag ' + statusClass(timeline.review_status)">
            {{ statusLabel(timeline.review_status) }}</span>
        </p>
        <p class="modal-reason" v-if="timeline.review_stage_count">
          审核流 {{ timeline.review_stage_count }} 级：
          <template v-for="(st, i) in (timeline.review_flow && timeline.review_flow.stages) || []"
                    :key="i">
            <span class="stage-chip"
                  :class="{ done: (timeline.stage_history || []).some(h => h.stage === i),
                            current: timeline.review_status === 'pending' && timeline.review_stage === i }">
              {{ i + 1 }}.{{ st.name }}（{{ roleLabel(st.role) }}）</span>
          </template>
          ｜ 申诉：{{ timeline.review_flow && timeline.review_flow.appeal_route === 'stage'
            ? '回到驳回级重审' : '复核员裁决' }}
        </p>
        <ul class="timeline-list">
          <li v-for="e in timeline.events" :key="e.seq" class="tl-item">
            <div class="tl-dot" :class="'dot-' + e.actor_role"></div>
            <div class="tl-body">
              <div class="tl-head">
                <b>{{ eventLabel(e.kind) }}</b>
                <span class="tl-actor">{{ e.actor || '—' }}（{{
                  { player: '玩家', reviewer: '初审员', moderator: '复核员',
                    system: '系统' }[e.actor_role] || e.actor_role }}）</span>
                <span class="tl-time">{{ fmtDateTime(e.created_at) }}</span>
              </div>
              <div v-if="eventDetail(e)" class="tl-detail">{{ eventDetail(e) }}</div>
            </div>
          </li>
        </ul>
        <div v-if="timeline.appeals && timeline.appeals.length" class="tl-appeals">
          <div v-for="a in timeline.appeals" :key="a.appeal_id" class="tl-appeal">
            第 {{ a.round }} 轮：{{ a.reason }}
            → <b :class="a.status === 'overturn' ? 'ok-text' : 'bad-text'">
              {{ a.status === 'overturn' ? '推翻原判' : '维持原判' }}</b>
            <span v-if="a.decided_by">（{{ a.decided_by }}：{{ a.decision_note }}）</span>
          </div>
        </div>
        <div class="modal-btns">
          <button class="btn ghost" @click="timeline = null">关闭</button>
        </div>
      </div>
    </div>
  </div>`,
};
