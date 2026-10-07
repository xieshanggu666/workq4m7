/* 视图：社区航线挑战（挑战列表 / 发布版本化关卡 / 审核队列） */
window.ChallengeSelect = {
  name: "ChallengeSelect",
  props: ["challenges", "bodies"],
  data() {
    return {
      mode: "list",          // list | publish | review
      reviewTab: "queue",    // queue=初审 appeals=复核 archive=终审管理 mine=我的成绩
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
      reviewerToken: Store.get("reviewerToken") || "",
      queueLevel: null,       // null=全部待审；数字=仅该审核级
      queueOverdueOnly: false,
      reviewerList: [],       // 可被指定到审核级的已注册审核账号
      actionErr: "",
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
    eventLabel(k) {
      return {
        submit: "玩家提交", review_approve: "终审通过·上榜",
        review_advance: "通过本级·流转下一级", review_reject: "审核驳回",
        appeal: "玩家申诉", appeal_uphold: "复核维持原判",
        appeal_overturn: "申诉推翻·继续审核", revoke: "复核撤销上榜",
        restore: "复核恢复上榜", legacy: "历史已审核（补登）",
      }[k] || k;
    },
    eventDetail(e) {
      const d = e.detail || {};
      if (e.kind === "submit")
        return `★${d.stars} · 服务端结算` + (d.level ? ` · 进入第 ${d.level} 级队列` : "");
      if (e.kind === "review_advance")
        return `第 ${d.from_level} 级「${d.from_stage}」通过 → 第 ${d.to_level} 级「${d.to_stage}」`
          + (d.note ? `：${d.note}` : "");
      if (e.kind === "review_approve")
        return `第 ${d.level} 级「${d.stage}」终审通过（共 ${d.total_levels} 级）`
          + (d.note ? `：${d.note}` : "");
      if (e.kind === "review_reject")
        return `第 ${d.level} 级「${d.stage}」驳回` + (d.note ? `：${d.note}` : "");
      if (e.kind === "appeal")
        return `第 ${d.round} 轮（原判 ${this.statusLabel(d.from_status)}`
          + (d.target_stage ? `，回到第 ${d.target_level} 级「${d.target_stage}」` : "，进复核队列")
          + `）：${d.reason || ""}`;
      if (e.kind === "appeal_uphold" || e.kind === "appeal_overturn")
        return `第 ${d.round} 轮` + (d.stage ? ` · 第 ${d.level} 级「${d.stage}」` : "")
          + (d.note ? `：${d.note}` : "");
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
        // null=使用默认（新建=默认单级；新版=沿用上版）；[]=显式配置的审核级列表
        pipelineEnabled: false,
        pipelineProvided: false,  // 是否随请求显式传 review_pipeline
        stages: [{ name: "", role: "reviewer", reviewers: [], hours: null }],
      };
    },
    async loadReviewers() {
      try {
        this.reviewerList = (await API.reviewers()).reviewers || [];
      } catch (e) { this.reviewerList = []; }
    },
    reviewerOptions(stage) {
      // 复核级只列复核员；初审级列出全部（moderator 可兜底任意级）
      return this.reviewerList.filter(r =>
        stage.role !== "moderator" || r.role === "moderator");
    },
    addStage() {
      this.form.stages.push(
        { name: "", role: this.form.stages.length ? "moderator" : "reviewer",
          reviewers: [], hours: null });
    },
    removeStage(i) {
      this.form.stages.splice(i, 1);
      this.form.stages.forEach((s, idx) => { if (!s.name) s.name = ""; });
    },
    onPipelineToggle(on) {
      this.form.pipelineEnabled = on;
      this.form.pipelineProvided = true;
      if (on && !this.form.stages.length) this.addStage();
    },
    onStageRole(stage) {
      // 角色切换后清掉不再合法的已选审核人
      const valid = new Set(this.reviewerOptions(stage).map(r => r.name));
      stage.reviewers = (stage.reviewers || []).filter(n => valid.has(n));
    },
    async openPublish(ch) {
      const f = this.blankForm();
      this.publishErr = "";
      this.loadReviewers();
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
        // 预填当前版本审核链：默认单级显示为“默认（不传）”，显式链展开编辑器
        const p = v.review_pipeline || { legacy: true, stages: [] };
        if (!p.legacy && p.stages && p.stages.length) {
          f.pipelineEnabled = true;
          f.pipelineProvided = true;
          f.stages = p.stages.map(s => ({
            name: s.name || "", role: s.role || "reviewer",
            reviewers: s.reviewers || [], hours: s.time_limit_hours ?? null,
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
      const payload = { ...def, unlock_rule: rule };
      // 审核链：仅在设计者显式操作过时随请求发送；
      // 关闭自定义 → 显式 null（回到默认单级）；开启 → 各级配置
      if (f.pipelineProvided) {
        if (!f.pipelineEnabled) {
          payload.review_pipeline = null;
        } else {
          if (!f.stages.length) {
            this.publishErr = "请至少配置一个审核级，或关闭「自定义多级审核」";
            return;
          }
          payload.review_pipeline = f.stages.map(s => ({
            name: (s.name || "").trim(),
            role: s.role,
            reviewers: s.reviewers || [],
            time_limit_hours: (s.hours === null || s.hours === "" || isNaN(s.hours))
              ? null : Number(s.hours),
          }));
        }
      }
      this.publishing = true;
      this.publishErr = "";
      try {
        if (f.mode === "create") {
          await API.createChallenge({ title: f.title, author: f.author, ...payload });
        } else {
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
          this.queue = (await API.reviewQueue(
            this.queueLevel, this.queueOverdueOnly)).submissions || [];
        } else if (tab === "appeals") {
          // 复核员旧队列：只看未路由的默认链路申诉
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
    stageOf(r) { return r.stage || null; },
    stageBadge(r) {
      if (!r || r.review_status !== "pending" || !r.stage) return "";
      return `第 ${r.review_level}/${r.total_levels} 级 · ${r.stage.name}`;
    },
    stageRoleLabel(role) {
      return role === "moderator" ? "复核级" : "初审级";
    },
    fmtDue(stage) {
      if (!stage || !stage.due_at) return "";
      const d = new Date(stage.due_at * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    reviewerNames(r) {
      const s = this.stageOf(r);
      return s && s.reviewers && s.reviewers.length ? s.reviewers.join("、") : "";
    },
    // 按当前级选择默认审核令牌：复核级/超时兜底用复核员令牌；初审级用审核令牌（可空）
    tokenFor(r) {
      const s = this.stageOf(r);
      if (s && (s.role === "moderator" || s.overdue)) return this.moderatorToken;
      return this.reviewerToken || null;
    },
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
        await API.reviewSubmission(
          rec.record_id, action, rec._note || "", rec.review_level,
          this.tokenFor(rec));
        await this.loadReviewTab("queue");
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
          <span class="ver-tag" v-if="c.review_pipeline && !c.review_pipeline.legacy">
            {{ c.review_pipeline.levels }} 级审核</span>
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

        <h3 class="form-h3">审核链
          <label class="pipeline-toggle">
            <input type="checkbox" :checked="form.pipelineEnabled"
                   @change="onPipelineToggle($event.target.checked)">
            自定义多级审核（按版本指定审核人与时限）
          </label>
        </h3>
        <p class="form-sub" v-if="!form.pipelineEnabled">
          {{ form.mode === "version"
             ? "未改动时新版本沿用上一版本审核链；勾选后可指定 1~5 个审核级。"
             : "默认单级（初审员一审，申诉进复核队列）；勾选后可指定 1~5 个审核级。" }}
        </p>
        <div v-if="form.pipelineEnabled" class="pipeline-list">
          <div v-for="(st, i) in form.stages" :key="i" class="stage-row">
            <div class="stage-head">
              <b>第 {{ i + 1 }} 级</b>
              <select v-model="st.role" @change="onStageRole(st)">
                <option value="reviewer">初审级（reviewer）</option>
                <option value="moderator">复核级（moderator）</option>
              </select>
              <input v-model.trim="st.name" maxlength="24" class="stage-name"
                     :placeholder="i === 0 ? '初审' : ('第 ' + (i + 1) + ' 级')">
              <button class="icon-btn danger" @click="removeStage(i)"
                      :disabled="form.stages.length <= 1">✕</button>
            </div>
            <div class="stage-body">
              <label class="stage-field">指定审核人（不选=该角色均可处理）
                <select v-model="st.reviewers" multiple size="3" class="stage-select">
                  <option v-for="rv in reviewerOptions(st)" :key="rv.name"
                          :value="rv.name">
                    {{ rv.name }}（{{ rv.role === "moderator" ? "复核员" : "初审员" }}）
                  </option>
                </select>
              </label>
              <label class="stage-field">处理时限（小时，留空=不限）
                <input type="number" v-model.number="st.hours" min="0.5" max="720"
                       step="0.5" class="num">
              </label>
            </div>
          </div>
          <button class="btn chip" @click="addStage"
                  :disabled="form.stages.length >= 5">+ 添加审核级（最多 5 级）</button>
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
          <span class="plan-count">初审上榜 · 玩家申诉 · 复核撤销/恢复 · 全链路可追溯</span>
        </h2>
        <div class="review-tabs">
          <button :class="{ active: reviewTab === 'queue' }"
                  @click="loadReviewTab('queue')">🗂 初审队列</button>
          <button :class="{ active: reviewTab === 'appeals' }"
                  @click="loadReviewTab('appeals')">⚖ 申诉复核
            <span v-if="appeals.length" class="badge">{{ appeals.length }}</span></button>
          <button :class="{ active: reviewTab === 'archive' }"
                  @click="loadReviewTab('archive')">📜 终审管理</button>
          <button :class="{ active: reviewTab === 'mine' }"
                  @click="loadReviewTab('mine')">🧑‍✈️ 我的成绩/申诉</button>
        </div>
        <p v-if="actionErr" class="form-err">⚠ {{ actionErr }}</p>

        <!-- 审核队列：按版本审核链逐级处理（含路由回该级的申诉重审） -->
        <template v-if="reviewTab === 'queue'">
          <div class="queue-toolbar">
            <label class="mod-token">审核令牌
              <input v-model.trim="reviewerToken"
                     @change="Store.set('reviewerToken', reviewerToken)"
                     placeholder="留空=内置初审员（复核级请在下方用复核员令牌）">
            </label>
            <label class="mod-token">
              <input type="checkbox" v-model="queueOverdueOnly"> 仅看超时
            </label>
            <div class="status-switch">
              <button :class="{ active: !queueLevel }" @click="queueLevel=null; loadReviewTab('queue')">全部级</button>
              <button v-for="lv in 5" :key="lv"
                      :class="{ active: queueLevel === lv }"
                      @click="queueLevel=lv; loadReviewTab('queue')">{{ lv }} 级</button>
            </div>
          </div>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">没有待审核的飞行记录。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row"
               :class="{ 'appeal-row': r.appeal_open, 'overdue-row': r.stage && r.stage.overdue }">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span v-if="r.stage" class="stage-tag">
                {{ stageBadge(r) }} · {{ stageRoleLabel(r.stage.role) }}
                <template v-if="reviewerNames(r)"> · {{ reviewerNames(r) }}</template>
              </span>
              <span v-if="r.stage && r.stage.overdue" class="overdue-tag">
                ⏰ 已超时（应于 {{ fmtDue(r.stage) }} 前处理）</span>
              <span v-else-if="r.stage && r.stage.due_at" class="due-tag">
                时限至 {{ fmtDue(r.stage) }}</span>
              <span v-if="r.appeal_open && r.appeal_routed" class="appeal-tag">
                第 {{ r.appeal_round }} 轮申诉·回到本级重审</span>
              <span v-else-if="r.appeal_open" class="appeal-tag">
                第 {{ r.appeal_round }} 轮申诉（复核队列处理）</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天 · {{ fmtDate(r.created_at) }}
              </div>
              <div v-if="r.appeal_open && r.appeal_routed" class="queue-meta appeal-reason">
                申诉理由：{{ r.appeal_reason }}（「通过」=推翻原判并继续流转；「驳回」=维持原判）
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._note" maxlength="200"
                   :placeholder="(r.stage && r.stage.final_level) ? '终审备注（可选）' : '本级备注（可选）'">
            <button class="btn mini approve"
                    @click="review(r, 'approve')">
              {{ r.stage && r.stage.final_level ? '✓ 终审通过'
                 : (r.appeal_open && r.appeal_routed ? '↺ 推翻·继续' : '↓ 通过本级') }}</button>
            <button class="btn mini reject"
                    @click="review(r, 'reject')">
              {{ r.appeal_open && r.appeal_routed ? '⊘ 维持原判' : '✕ 驳回' }}</button>
            <button class="btn mini ghost" @click="showTimeline(r.record_id)">链路</button>
          </div>
        </template>

        <!-- 申诉复核（仅复核员；未配置多级审核链的默认链路申诉） -->
        <template v-if="reviewTab === 'appeals'">
          <label class="mod-token">复核员令牌
            <input v-model.trim="moderatorToken"
                   @change="Store.set('modToken', moderatorToken)"
                   placeholder="local-moderator（本地单机默认）">
          </label>
          <p class="form-sub">
            配置了多级审核链的版本，玩家申诉会回到做出原判的那一级审核队列（见「初审队列」）；
            这里只受理默认单级链路的申诉。
          </p>
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
              <span v-if="r.appeal_open" class="appeal-tag">
                第 {{ r.appeal_rounds }} 轮申诉复核中
                <template v-if="r.appeal_target_level">
                  · 第 {{ r.appeal_target_level }} 级重审</template>
              </span>
              <span v-else-if="r.review_status === 'pending' && r.stage" class="stage-tag">
                审核中 {{ r.review_level }}/{{ r.total_levels }} 级 · {{ r.stage.name }}
                <span v-if="r.stage.overdue" class="overdue-tag">已超时</span>
              </span>
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
