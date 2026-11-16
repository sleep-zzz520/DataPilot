<template>
  <div
    class="analysis-workspace"
    :class="{ 'sidebar-is-collapsed': sidebarCollapsed }"
  >
    <WorkspaceSidebar
      :sessions="sessions"
      :session-id="store.sessionId"
      :collapsed="sidebarCollapsed"
      :username="username"
      @toggle="sidebarCollapsed = !sidebarCollapsed"
      @new-chat="newChat"
      @select-session="loadSession"
      @account="accountActions"
    />
    <button
      v-if="!sidebarCollapsed"
      class="sidebar-scrim"
      aria-label="关闭侧栏"
      @click="sidebarCollapsed = true"
    ></button>
    <div class="analysis-main">
      <header class="conversation-header">
        <button
          v-if="sidebarCollapsed"
          class="icon-button sidebar-expand-control"
          aria-label="展开侧栏"
          @click="sidebarCollapsed = false"
        >
          <AppIcon name="panel" />
        </button>
        <div v-if="expandedResult" class="result-heading">
          <button class="text-button" @click="closeResult">
            <AppIcon name="back" :size="16" />返回对话
          </button>
          <h2>{{ expandedResult.title }}</h2>
        </div>
        <div class="conversation-header-column">
          <div v-if="!expandedResult" class="conversation-heading">
            <h1 v-if="messages.length">{{ currentTitle }}</h1>
          </div>
        </div>
        <div class="conversation-header-actions">
          <details
            v-if="store.dbList.length"
            class="conversation-menu header-db-menu"
          >
            <summary class="text-button db-summary">
              <AppIcon name="database" :size="14" /><span>{{
                store.currentDb?.name || "选择数据库"
              }}</span
              ><AppIcon name="chevron" :size="12" class="menu-chevron" />
            </summary>
            <div class="popover-menu">
              <button
                v-for="db in store.dbList"
                :key="db.id"
                :disabled="loading"
                @click="
                  store.currentDbId = db.id;
                  closeMenu($event);
                "
              >
                {{ db.name }}</button
              ><button
                class="menu-action"
                :disabled="!store.currentDbId || loading"
                @click="
                  showSchema();
                  closeMenu($event);
                "
              >
                查看表结构
              </button>
            </div>
          </details>
          <details v-if="currentSession" class="conversation-menu">
            <summary class="icon-button" aria-label="会话操作">
              <AppIcon name="more" />
            </summary>
            <div class="popover-menu">
              <button
                @click="
                  startRename(currentSession);
                  closeMenu($event);
                "
              >
                重命名</button
              ><button
                class="danger-text"
                :disabled="loading"
                @click="removeSession(store.sessionId)"
              >
                删除会话
              </button>
            </div>
          </details>
        </div>
      </header>
      <div v-if="editingId" class="inline-rename">
        <form @submit.prevent="saveRename(currentSession)">
          <input
            ref="renameInputEl"
            v-model="editTitle"
            aria-label="会话名称"
            maxlength="100"
            @keydown.esc="cancelRename"
          /><button class="text-button" type="button" @click="cancelRename">
            取消</button
          ><button class="text-button" type="submit">保存</button>
        </form>
      </div>
      <section
        v-if="expandedResult"
        class="expanded-result"
        aria-label="展开的分析结果"
      >
        <div class="result-body">
          <DataTable
            v-if="expandedResult.type === 'table'"
            :table="expandedResult.value"
          /><ChartView
            v-else-if="expandedResult.type === 'chart'"
            :chart-config="expandedResult.value.chart"
            :image-base64="expandedResult.value.image"
            expanded
          />
          <pre v-else class="expanded-sql">{{ expandedResult.value }}</pre>
        </div>
      </section>
      <div
        v-show="!expandedResult"
        class="conversation-content"
        :class="{ 'is-empty': !messages.length }"
      >
        <div ref="msgsEl" class="conversation-scroll">
          <div class="conversation-column">
            <section v-if="!messages.length" class="conversation-welcome">
              <span class="welcome-symbol"
                ><AppIcon name="brand" :size="34"
              /></span>
              <h2>
                {{
                  store.ready ? "今天想分析什么？" : "准备好数据，开始分析。"
                }}
              </h2>
              <p v-if="store.ready">从数据库出发，或添加一份数据文件。</p>
              <p v-else>连接模型与数据库，开始你的第一次分析。</p>
              <router-link v-if="!store.ready" class="setup-link" to="/config"
                >前往配置<AppIcon name="chevron" :size="14"
              /></router-link>
            </section>
            <article
              v-for="(m, i) in messages"
              :key="m._key || i"
              class="analysis-message"
              :class="m.role"
            >
              <div v-if="m.role === 'user'" class="user-message">
                {{ m.text }}
              </div>
              <template v-else>
                <div class="assistant-identity">
                  <AppIcon name="brand" :size="16" /><span>数据分析 Agent</span>
                </div>
                <AgentTrace v-if="m.trace?.length" :entries="m.trace" compact />
                <div
                  v-if="
                    isRunningMessage(m, i) &&
                    !m.text &&
                    !m.trace?.length &&
                    !m.confirmation &&
                    !m.error
                  "
                  class="run-status"
                  role="status"
                >
                  <span class="activity-indicator"></span
                  >{{
                    m.plan?.steps?.find((s) => s.status === "running")?.title ||
                    "正在准备分析"
                  }}
                </div>
                <MarkdownContent v-if="m.text" :content="m.text" />
                <div v-if="m.confirmation" class="hitl-card">
                  <h3>执行前确认</h3>
                  <dl>
                    <template v-if="m.confirmation.reason_labels?.length"
                      ><dt>触发原因</dt>
                      <dd>
                        {{ m.confirmation.reason_labels.join("；") }}
                      </dd></template
                    ><template v-if="m.confirmation.estimated_cost"
                      ><dt>预计成本</dt>
                      <dd>
                        {{ m.confirmation.estimated_cost.model_calls }} ·
                        {{ m.confirmation.estimated_cost.token_range }}
                      </dd></template
                    >
                    <dt>数据范围</dt>
                    <dd>
                      {{ (m.confirmation.data_scope || []).join("；") }}
                    </dd>
                    <dt>执行步骤</dt>
                    <dd>{{ (m.confirmation.steps || []).join(" → ") }}</dd>
                  </dl>
                  <p v-if="m.confirmation.risks?.length" class="danger-text">
                    {{ m.confirmation.risks.join("；") }}
                  </p>
                  <p v-if="m.confirmation.mode === 'clarification'">
                    {{ m.confirmation.prompt }}
                  </p>
                  <div
                    v-else-if="m.confirmation.status === 'pending'"
                    class="form-actions"
                  >
                    <button
                      class="text-button"
                      :disabled="loading"
                      @click="decideConfirmation(m, 'reject')"
                    >
                      拒绝</button
                    ><button
                      class="primary-button"
                      :disabled="loading"
                      @click="decideConfirmation(m, 'approve')"
                    >
                      确认执行
                    </button>
                  </div>
                  <p v-else>本次分析已拒绝，未执行查询。</p>
                </div>
                <details v-if="m.plan" class="analysis-plan">
                  <summary>分析计划</summary>
                  <p>{{ m.plan.goal }}</p>
                  <div
                    v-for="step in m.plan.steps || []"
                    :key="step.id"
                    class="plan-step"
                  >
                    <span
                      class="plan-step-status"
                      :class="`is-${step.status}`"
                      role="img"
                      :aria-label="planStepStatus(step.status).label"
                      :title="planStepStatus(step.status).label"
                    >
                      <AppIcon
                        :name="planStepStatus(step.status).icon"
                        :size="16"
                      />
                    </span>
                    <span class="plan-step-title">{{ step.title }}</span>
                  </div>
                  <p
                    v-for="issue in m.plan.quality_issues || []"
                    :key="issue"
                    class="danger-text"
                  >
                    {{ issue }}
                  </p>
                </details>
                <p v-if="m.plan?.clarification_needed" class="analysis-notice">
                  {{ m.plan.clarification_question }}
                </p>
                <p
                  v-for="ev in (m.plan?.evidence || []).filter(
                    (e) => e.quality_issues?.length,
                  )"
                  :key="ev.source"
                  class="analysis-notice"
                >
                  {{ ev.source }}：{{ ev.quality_issues.join("；") }}
                </p>
                <section
                  v-for="(v, vi) in m.visuals || []"
                  :key="'v' + vi"
                  class="inline-result"
                >
                  <div class="inline-result-heading">
                    <span
                      >图表{{ m.visuals.length > 1 ? ` ${vi + 1}` : "" }}</span
                    ><button
                      class="text-button"
                      @click="openResult('chart', v, '分析图表')"
                    >
                      <AppIcon name="expand" :size="14" />放大
                    </button>
                  </div>
                  <ChartView :chart-config="v.chart" :image-base64="v.image" />
                </section>
                <section
                  v-for="(tbl, ti) in m.tables || []"
                  :key="'t' + ti"
                  class="inline-result"
                >
                  <div class="inline-result-heading">
                    <span
                      >{{ tbl.title || "数据预览"
                      }}<span class="muted">
                        · {{ tbl.rows?.length || 0 }} 行</span
                      ></span
                    ><button
                      class="text-button"
                      @click="openResult('table', tbl, tbl.title || '分析数据')"
                    >
                      <AppIcon name="expand" :size="14" />展开
                    </button>
                  </div>
                  <DataTable :table="tbl" :preview-limit="6" />
                </section>
                <ErrorBanner
                  v-if="m.error"
                  :error="m.error"
                  @go-config="$router.push('/config')"
                />
                <div
                  v-if="
                    m.text || m.error || m.tables?.length || m.visuals?.length
                  "
                  class="message-actions"
                >
                  <button
                    v-if="m.sql"
                    class="text-button"
                    @click="openResult('sql', m.sql, '生成的 SQL')"
                  >
                    查看 SQL</button
                  ><button
                    class="icon-button"
                    title="复制回答"
                    @click="copyMessage(m)"
                  >
                    <AppIcon name="copy" :size="15" />
                  </button>
                </div>
              </template>
            </article>
          </div>
        </div>
        <div class="composer-region">
          <div class="composer-column">
            <div v-if="files.length" class="selected-files">
              <span v-for="f in files" :key="f.file_id" class="file-chip"
                ><AppIcon name="file" :size="14" /><span>{{ f.name }}</span
                ><button
                  class="icon-button"
                  :disabled="loading"
                  :aria-label="`移除${f.name}`"
                  @click="removeFile(f.file_id)"
                >
                  <AppIcon name="close" :size="12" /></button
              ></span>
            </div>
            <form
              class="analysis-composer"
              :class="{ focused: inputFocused }"
              @submit.prevent="send"
            >
              <textarea
                ref="textareaRef"
                v-model="input"
                aria-label="分析问题"
                placeholder="问数据一个问题…"
                rows="2"
                @keydown.enter.exact="onEnter"
                @focus="inputFocused = true"
                @blur="inputFocused = false"
                @input="autoResize"
              />
              <div class="composer-actions">
                <input
                  ref="attachmentInput"
                  type="file"
                  hidden
                  :disabled="loading || uploading"
                  @change="onFile"
                /><button
                  type="button"
                  class="attachment-control"
                  :disabled="loading || uploading"
                  title="添加文件"
                  aria-label="添加文件"
                  @click="attachmentInput?.click()"
                >
                  <AppIcon name="attach" :size="17" /></button
                ><span
                  class="composer-actions-spacer"
                  aria-hidden="true"
                ></span>
                <details
                  v-if="store.llmList.length"
                  class="conversation-menu composer-model-menu"
                >
                  <summary class="text-button model-summary">
                    <span>{{ store.currentLlm?.name || "选择模型" }}</span
                    ><AppIcon name="chevron" :size="12" class="menu-chevron" />
                  </summary>
                  <div class="popover-menu model-menu">
                    <button
                      type="button"
                      v-for="llm in store.llmList"
                      :key="llm.id"
                      :disabled="loading"
                      @click="
                        store.currentLlmId = llm.id;
                        closeMenu($event);
                      "
                    >
                      {{ llm.name }}
                    </button>
                  </div>
                </details>
                <button
                  type="submit"
                  class="send-button"
                  :disabled="loading || uploading || !input.trim()"
                  :aria-label="loading ? '正在分析' : '发送问题'"
                >
                  <AppIcon :name="loading ? 'loader' : 'arrow'" :size="20" />
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, inject, nextTick, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useConfigStore } from "../stores/config.js";
import * as chatApi from "../api/chat.js";
import ChartView from "../components/ChartView.vue";
import DataTable from "../components/DataTable.vue";
import ErrorBanner from "../components/ErrorBanner.vue";
import MarkdownContent from "../components/MarkdownContent.vue";
import AgentTrace from "../components/AgentTrace.vue";
import AppIcon from "../components/AppIcon.vue";
import WorkspaceSidebar from "../components/WorkspaceSidebar.vue";

const router = useRouter();
const store = useConfigStore();
const messages = ref([]);
const input = ref("");
const loading = ref(false);
const files = ref([]);
const msgsEl = ref(null);
const sessions = ref([]);
const sidebarCollapsed = ref(
  typeof window !== "undefined" && window.innerWidth < 760,
);
const inputFocused = ref(false);
const textareaRef = ref(null);
// 流式输出是否已开始（开始后隐藏打字指示器，避免与流式文本重叠）
const streamingActive = ref(false);
const sendingSessionId = ref(null); // 发送中的会话：切换会话后隐藏加载动画
// 发送中未落库的本地消息（sessionId -> 消息数组）：切走再切回不丢用户气泡
const pendingMsgs = ref(new Map());
let msgSeq = 0;
const mkMsg = (role, text = "") => ({
  role,
  text,
  timestamp: new Date().toISOString(),
  _key: `m${++msgSeq}`,
  trace: role === "assistant" ? [] : undefined,
});

// 会话加载竞态保护：快速切换会话时，只应用最后一次请求的结果，
// 避免慢响应覆盖新会话的消息（串话）。
let loadSeq = 0;

// 解析图表配置（兼容字符串和对象）
function parseChartConfig(raw) {
  if (!raw) return null;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }
  return raw;
}

onMounted(async () => {
  if (!store.llmList.length) await store.loadAll();
  await refreshSessions();
  // 若有历史 sessionId，自动加载该会话消息
  if (store.sessionId) {
    await loadSession(store.sessionId, /* silent */ true);
  }
});

const scroll = () =>
  nextTick(() => {
    if (msgsEl.value) msgsEl.value.scrollTop = msgsEl.value.scrollHeight;
  });

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000)
    return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

async function refreshSessions() {
  try {
    sessions.value = await chatApi.listSessions();
  } catch (_) {
    sessions.value = [];
  }
}

async function loadSession(sid, silent = false) {
  if (sid === store.sessionId && messages.value.length > 0 && !silent) return;
  const mySeq = ++loadSeq;
  store.setSession(sid);
  try {
    const data = await chatApi.getSession(sid);
    if (mySeq !== loadSeq) return; // 已有更新的加载请求，丢弃本次结果
    const traces = data.traces || [];
    let traceIdx = 0;
    messages.value = (data.messages || []).map((m) => ({
      role: m.role,
      text: m.text || "",
      sql: m.sql || null,
      tables: m.tables || (m.table ? [m.table] : []),
      visuals: (
        m.visuals || (m.chart ? [{ chart: m.chart, image: m.imageBase64 }] : [])
      ).map((v) => ({
        chart: parseChartConfig(v.chart),
        image: v.image || null,
      })),
      // 轨迹按轮次与 assistant 消息一一对应（后端每轮都落一条，可能为空数组）
      trace: m.role === "assistant" ? traces[traceIdx++] || [] : undefined,
      timestamp: m.timestamp || new Date().toISOString(),
    }));
    // 合并发送中未落库的本地消息（用户气泡不因切会话丢失）
    const pending = pendingMsgs.value.get(sid) || [];
    if (pending.length) messages.value = messages.value.concat(pending);
    scroll();
  } catch (err) {
    if (mySeq !== loadSeq) return;
    // 会话后端还不存在（发送中跳走又回来）：展示本地 pending 消息，避免"会话消失"
    const pending = pendingMsgs.value.get(sid) || [];
    if (pending.length) {
      messages.value = [...pending];
      scroll();
      return;
    }
    if (!silent)
      messages.value.push({
        role: "assistant",
        error: err,
        timestamp: new Date().toISOString(),
      });
  }
}

async function removeSession(sid) {
  if (!confirm("确定删除该会话？此操作不可撤销。")) return;
  try {
    await chatApi.deleteSession(sid);
    if (sid === store.sessionId) {
      messages.value = [];
      store.newSession();
    }
    await refreshSessions();
  } catch (err) {
    messages.value.push({ role: "assistant", error: err });
  }
}

// ── 会话重命名（内联编辑） ──────────────────────────────────────────────
const editingId = ref(null);
const editTitle = ref("");
const renameInputEl = ref(null);

function startRename(s) {
  editingId.value = s.id;
  editTitle.value = s.title;
  nextTick(() => renameInputEl.value?.focus());
}

async function saveRename(s) {
  if (editingId.value !== s.id) return; // 已退出编辑态（如 ESC 或已保存）
  const title = editTitle.value.trim();
  editingId.value = null;
  if (!title || title === s.title) return;
  try {
    await chatApi.renameSession(s.id, title);
    s.title = title; // 本地立即生效
  } catch (err) {
    messages.value.push({ role: "assistant", error: err });
  }
}

function cancelRename() {
  editingId.value = null;
}

async function onFile(e) {
  const file = e.target.files?.[0];
  if (!file || uploading.value || loading.value) return;
  uploading.value = true;
  try {
    const res = await chatApi.upload(file);
    files.value.push({ file_id: res.file_id, name: file.name });
  } catch (err) {
    messages.value.push({ role: "assistant", error: err });
    scroll();
  } finally {
    uploading.value = false;
    e.target.value = "";
  }
}

function removeFile(file_id) {
  files.value = files.value.filter((f) => f.file_id !== file_id);
}

function applyDone(target, res) {
  target.plan = res.plan || target.plan || null;
  target.trace = res.trace || target.trace || [];
  target.sql = res.sql || null;
  target.tables = res.tables || (res.table ? [res.table] : []);
  target.visuals = (
    res.visuals || (res.chart ? [{ chart: res.chart }] : [])
  ).map((v) => ({ chart: parseChartConfig(v.chart), image: v.image || null }));
  target.confirmation = res.confirmation || null;
  if (res.confirmation) {
    target.text = res.reply || target.text || "执行前需要你的确认。";
    return;
  }
  if (target.text && target.text.includes("<!--IMAGE_BASE64:")) {
    target.text = target.text.replace(/<!--IMAGE_BASE64:.*?-->/g, "").trim();
  }
  if (!target.text.trim()) target.text = res.reply || "(无文本回复)";
}

async function decideConfirmation(msg, decision) {
  const approvalId = msg.confirmation?.approval_id;
  if (!approvalId || loading.value || !store.ready) return;
  const sidAtSend = store.sessionId;
  loading.value = true;
  sendingSessionId.value = sidAtSend;
  streamingActive.value = false;
  msg.text = "";
  let streamError = false;
  try {
    await chatApi.streamChat(
      {
        message: msg._approvalText,
        session_id: sidAtSend,
        llm_config_id: store.currentLlmId,
        db_config_id: store.currentDbId,
        file_ids: msg._approvalFileIds || [],
        approval_id: approvalId,
        approval_decision: decision,
      },
      {
        onDelta: (t) => {
          if (store.sessionId === sidAtSend) {
            streamingActive.value = true;
            msg.text += t;
          }
        },
        onTrace: (entries) => {
          msg.trace = (msg.trace || []).concat(entries);
        },
        onPlan: (plan) => {
          msg.plan = plan;
        },
        onDone: (res) => {
          applyDone(msg, res);
        },
        onError: (err) => {
          streamError = true;
          msg.error = err;
        },
      },
    );
    if (!streamError && !msg.confirmation && store.sessionId === sidAtSend) {
      pendingMsgs.value.delete(sidAtSend);
      await loadSession(sidAtSend, true);
    }
    await refreshSessions();
  } catch (err) {
    msg.error = err;
  } finally {
    loading.value = false;
    streamingActive.value = false;
    sendingSessionId.value = null;
    scroll();
  }
}

async function send() {
  const text = input.value.trim();
  if (!text || loading.value || uploading.value) return;
  if (!store.ready) {
    router.push("/config");
    return;
  }
  const sidAtSend = store.sessionId; // 记录发送时的会话，防止响应回来时已切换会话
  // Bug2 修复：发消息立即乐观更新会话列表（时间=现在并置顶），
  // 回复完成后 refreshSessions 会用后端真实 updated_at 校准
  const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
  const curSession = sessions.value.find((x) => x.id === store.sessionId);
  if (curSession) {
    curSession.updated_at = nowLocal;
    sessions.value.sort((a, b) =>
      String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
    );
  }
  input.value = "";
  loading.value = true;
  scroll();
  sendingSessionId.value = store.sessionId;
  // 重置textarea高度
  if (textareaRef.value) {
    textareaRef.value.style.height = "auto";
  }
  // 用户消息 + 预创建 assistant 占位（带 _key 身份），并缓存到本地（切会话不丢）
  const userMsg = mkMsg("user", text);
  const assistantMsg = mkMsg("assistant", "");
  messages.value.push(userMsg, assistantMsg);
  const pending = pendingMsgs.value.get(store.sessionId) || [];
  pending.push(userMsg, assistantMsg);
  pendingMsgs.value.set(store.sessionId, pending);
  streamingActive.value = false;
  let streamError = false;
  let awaitingConfirmation = false;
  try {
    await chatApi.streamChat(
      {
        message: text,
        session_id: store.sessionId,
        llm_config_id: store.currentLlmId,
        db_config_id: store.currentDbId,
        file_ids: files.value.map((f) => f.file_id),
      },
      {
        onDelta: (t) => {
          if (store.sessionId !== sidAtSend) return;
          const target = messages.value.find(
            (m) => m._key === assistantMsg._key,
          );
          if (!target) return;
          streamingActive.value = true;
          target.text += t;
        },
        onTrace: (entries) => {
          if (store.sessionId !== sidAtSend) return;
          const target = messages.value.find(
            (m) => m._key === assistantMsg._key,
          );
          if (!target) return;
          target.trace = (target.trace || []).concat(entries);
        },
        onPlan: (plan) => {
          if (store.sessionId !== sidAtSend) return;
          const target = messages.value.find(
            (m) => m._key === assistantMsg._key,
          );
          if (target) target.plan = plan;
        },
        onDone: (res) => {
          // 发送期间跳走了（如去配置中心）：也把会话 id 固化到 store，
          // 回来时按真实 id 加载（此时后端已完成 persist）；用户已切到其他
          // 会话则不覆盖（store.sessionId 已变）
          if (res.session_id && store.sessionId === sidAtSend)
            store.setSession(res.session_id);
          if (store.sessionId !== sidAtSend) return; // 已切换会话：不显示，消息已落库
          const msg = messages.value.find((m) => m._key === assistantMsg._key);
          if (!msg) return;
          if (res.confirmation) {
            awaitingConfirmation = true;
            msg._approvalText = text;
            msg._approvalFileIds = files.value.map((f) => f.file_id);
          }
          applyDone(msg, res);
        },
        onError: (err) => {
          streamError = true;
          if (store.sessionId === sidAtSend) {
            const target = messages.value.find(
              (m) => m._key === assistantMsg._key,
            );
            if (target) target.error = err;
          }
        },
      },
    );
    // 后端在 done 前已完成持久化；重新加载一次当前会话，确保大响应、
    // 代理截断或 SSE done 丢失时，最终消息也能立即进入当前视图。
    if (!awaitingConfirmation) pendingMsgs.value.delete(sidAtSend);
    if (
      !streamError &&
      !awaitingConfirmation &&
      store.sessionId === sidAtSend
    ) {
      await loadSession(sidAtSend, true);
    }
    // 发送成功后刷新会话列表(标题/时间可能更新)
    await refreshSessions();
  } catch (err) {
    if (store.sessionId === sidAtSend) {
      const target = messages.value.find((m) => m._key === assistantMsg._key);
      if (target) target.error = err;
    }
  } finally {
    loading.value = false;
    streamingActive.value = false;
    sendingSessionId.value = null;
    scroll();
  }
}

async function newChat() {
  editingId.value = null;
  expandedResult.value = null;
  loadSeq++; // 使任何挂起的 loadSession 失效，避免旧响应覆盖空会话
  if (store.sessionId) {
    try {
      await chatApi.clearChat(store.sessionId);
    } catch (_) {
      /* 忽略清除失败 */
    }
  }
  store.newSession();
  messages.value = [];
  files.value = [];
}

async function showSchema() {
  if (!store.currentDbId) {
    router.push("/config");
    return;
  }
  try {
    const res = await chatApi.schema(store.currentDbId);
    const text =
      (res.tables || [])
        .map((t) => `• ${t.name}${t.comment ? "  -- " + t.comment : ""}`)
        .join("\n") || "(无表)";
    messages.value.push({
      role: "assistant",
      text: "当前库表结构:\n" + text,
      timestamp: new Date().toISOString(),
    });
  } catch (err) {
    messages.value.push({
      role: "assistant",
      error: err,
      timestamp: new Date().toISOString(),
    });
  }
  scroll();
}

function autoResize() {
  if (!textareaRef.value) return;
  textareaRef.value.style.height = "auto";
  textareaRef.value.style.height =
    Math.min(textareaRef.value.scrollHeight, 200) + "px";
}

const uploading = ref(false);
const attachmentInput = ref(null);
const accountActions = inject("accountActions", () => {});
const username = localStorage.getItem("username") || "账户";
const currentSession = computed(() =>
  sessions.value.find((s) => s.id === store.sessionId),
);
const currentTitle = computed(() => currentSession.value?.title || "新对话");
const expandedResult = ref(null);
function closeMenu(event) {
  event.target.closest("details")?.removeAttribute("open");
}
function openResult(type, value, title) {
  expandedResult.value = { type, value, title };
}
function closeResult() {
  expandedResult.value = null;
}
async function copyMessage(m) {
  if (m.text) await navigator.clipboard.writeText(m.text);
}
function isRunningMessage(m, i) {
  return (
    loading.value &&
    sendingSessionId.value === store.sessionId &&
    i === messages.value.length - 1
  );
}
function onEnter(event) {
  if (event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  send();
}
const planStatuses = {
  pending: { icon: "circle", label: "等待执行" },
  running: { icon: "loader", label: "正在执行" },
  succeeded: { icon: "check-circle", label: "已执行" },
  failed: { icon: "x-circle", label: "执行失败" },
  skipped: { icon: "minus-circle", label: "已跳过" },
};
function planStepStatus(status) {
  return planStatuses[status] || { icon: "circle", label: "未知状态" };
}
</script>
