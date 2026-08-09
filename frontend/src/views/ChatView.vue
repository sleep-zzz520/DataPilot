<template>
  <div class="chat-wrap">
    <!-- 左侧：会话列表侧边栏 -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="sidebar-header">
        <button class="new-btn" @click="newChat">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新对话
        </button>
        <button class="collapse-btn" @click="sidebarCollapsed = !sidebarCollapsed" title="折叠侧边栏">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="11 17 6 12 11 7"/><polyline points="18 17 13 12 18 7"/></svg>
        </button>
      </div>
      <div class="sess-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="sess-item"
          :class="{ active: s.id === store.sessionId }"
          @click="loadSession(s.id)"
        >
          <svg class="sess-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <div class="sess-content">
            <!-- 编辑态：内联输入框 -->
            <div v-if="editingId === s.id" class="sess-edit" @click.stop>
              <input
                v-model="editTitle"
                ref="renameInputEl"
                class="sess-edit-input"
                placeholder="输入新标题"
                @keydown.enter="saveRename(s)"
                @keydown.esc="cancelRename"
                @blur="saveRename(s)"
              />
            </div>
            <template v-else>
              <div class="sess-title">{{ s.title }}</div>
              <div class="sess-meta">{{ formatTime(s.updated_at) }}</div>
            </template>
          </div>
          <button class="op-btn" @click.stop="startRename(s)" title="重命名">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
          <button class="del-btn" @click.stop="removeSession(s.id)" title="删除">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
        <div v-if="!sessions.length" class="sess-empty">
          <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.3"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <span>暂无历史会话</span>
        </div>
      </div>
    </aside>

    <!-- 折叠时的展开按钮 -->
    <button v-if="sidebarCollapsed" class="expand-btn" @click="sidebarCollapsed = false" title="展开侧边栏">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></svg>
    </button>

    <!-- 右侧:对话主区域 -->
    <div class="chat">
      <!-- 顶部工具栏 -->
      <div class="bar">
        <div class="bar-selects">
          <label class="select-label">
            <span class="select-label-text">LLM</span>
            <select v-model="store.currentLlmId" class="bar-select">
              <option v-for="it in store.llmList" :key="it.id" :value="it.id">{{ it.name }}{{ it.is_default ? '(默认)' : '' }}</option>
            </select>
          </label>
          <label class="select-label">
            <span class="select-label-text">数据库</span>
            <select v-model="store.currentDbId" class="bar-select">
              <option v-for="it in store.dbList" :key="it.id" :value="it.id">{{ it.name }}{{ it.is_default ? '(默认)' : '' }}</option>
            </select>
          </label>
        </div>
        <div class="bar-actions">
          <button class="ghost" @click="showSchema">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
            查看表结构
          </button>
          <router-link v-if="!store.ready" to="/config" class="warnlink">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            请先完成配置
          </router-link>
        </div>
      </div>
    
      <!-- 上传区 -->
      <div class="upload">
        <label class="upload-btn">
          <input type="file" @change="onFile" style="display:none" />
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          上传文件
        </label>
        <span v-for="f in files" :key="f.file_id" class="chip">
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          {{ f.name }}
          <button class="chip-close" @click.stop="removeFile(f.file_id)" title="删除">
            <svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </span>
      </div>
    
      <!-- 消息流 -->
      <div class="msgs" ref="msgsEl">
        <div v-if="!messages.length" class="empty">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24" width="64" height="64" fill="none" stroke="url(#emptyGrad)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <defs>
                <linearGradient id="emptyGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#1F1F1F" stop-opacity="0.6"/>
                  <stop offset="100%" stop-color="#444444" stop-opacity="0.6"/>
                </linearGradient>
              </defs>
              <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
            </svg>
          </div>
          <p v-if="store.ready" class="empty-text">配置就绪,试着问:"上个月各状态的订单数?" 或上传一个 CSV 让我分析。</p>
          <p v-else class="empty-text">还没有可用配置,<router-link to="/config">去配置页</router-link> 添加 LLM 与数据库。</p>
        </div>
            
        <div v-for="(m, i) in messages" :key="i" class="msg-wrapper" :class="m.role"
             v-show="!(m.role === 'assistant' && !m.text && !m.error && !(m.visuals || []).length && !(m.tables || []).length)">
          <div class="msg">
            <!-- AI头像 - 左侧32px圆形 -->
            <div v-if="m.role === 'assistant'" class="avatar avatar-assistant">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
            </div>
                
            <!-- 消息内容 -->
            <div class="msg-content">
              <div class="msg-bubble" :class="m.role">
                <!-- 用户消息文本（纯文本） -->
                <div v-if="m.role === 'user' && m.text" class="text" :class="m.role">{{ m.text }}</div>

                <!-- AI 回复：Markdown 渲染为美观格式 -->
                <MarkdownContent v-if="m.role === 'assistant' && m.text" :content="m.text" />
                <details v-if="m.role === 'assistant' && m.plan" class="analysis-plan">
                  <summary>分析计划</summary>
                  <div class="plan-goal">{{ m.plan.goal }}</div>
                  <div v-for="step in (m.plan.steps || [])" :key="step.id" class="plan-step">
                    <span :class="'plan-status-' + step.status">{{ step.status === 'succeeded' ? '✓' : step.status === 'failed' ? '!' : step.status === 'running' ? '⟳' : '○' }}</span>{{ step.title }}
                  </div>
                  <div v-if="(m.plan.quality_issues || []).length" class="plan-warning">{{ m.plan.quality_issues.join('；') }}</div>
                  <div v-if="m.plan.clarification_needed" class="plan-warning">{{ m.plan.clarification_question }}</div>
                  <div v-for="ev in (m.plan.evidence || [])" :key="ev.created_at + ev.source" class="plan-evidence">
                    证据：{{ ev.source }}，{{ ev.row_count }} 行{{ ev.quality_issues?.length ? '，存在质量问题' : '' }}
                  </div>
                </details>
                    
                <!-- AI回复的SQL/表格/图表/错误 -->
                <template v-if="m.role === 'assistant'">
                  <!-- Agent 轨迹：实时展示本轮工具调用链（简历演示亮点） -->
                  <AgentTrace v-if="(m.trace || []).length" :entries="m.trace" />
                  <details v-if="m.sql" class="sql"><summary>查看生成的 SQL</summary><pre>{{ m.sql }}</pre></details>
                  <template v-for="(tbl, ti) in (m.tables || [])" :key="'t' + ti">
                    <DataTable :table="tbl" />
                  </template>
                  <template v-for="(v, vi) in (m.visuals || [])" :key="'v' + vi">
                    <ChartView :chart-config="v.chart" :image-base64="v.image" />
                  </template>
                  <ErrorBanner v-if="m.error" :error="m.error" @go-config="$router.push('/config')" />
                </template>
              </div>
                  
              <!-- 时间戳 - 外置于气泡下方 -->
              <div v-if="m.timestamp" class="msg-timestamp" :class="m.role">
                {{ formatTime(m.timestamp) }}
              </div>
                  
              <!-- 操作按钮 - hover渐显 -->
              <div class="msg-actions" :class="m.role">
                <button class="action-btn" title="复制">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </button>
                <button class="action-btn" title="点赞">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>
                </button>
              </div>
            </div>
                
            <!-- 用户头像 - 右侧32px圆形 -->
            <div v-if="m.role === 'user'" class="avatar avatar-user">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            </div>
          </div>
        </div>
            
        <!-- 加载态:呼吸灯效果(仅当流式输出尚未开始 且 当前仍在本会话) -->
        <div v-if="loading && !streamingActive && sendingSessionId === store.sessionId" class="msg-wrapper assistant">
          <div class="msg">
            <div class="avatar avatar-assistant">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
            </div>
            <div class="msg-content">
              <div class="msg-bubble assistant">
                <div class="typing-indicator">
                  <span class="dot"></span>
                  <span class="dot"></span>
                  <span class="dot"></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    
      <!-- 输入区域 - 底部固定卡片式 -->
      <div class="inputbar">
        <div class="input-card" :class="{ focused: inputFocused }">
          <textarea
            v-model="input"
            @keydown.enter.exact.prevent="send"
            @focus="inputFocused = true"
            @blur="inputFocused = false"
            @input="autoResize"
            ref="textareaRef"
            placeholder="输入分析问题,Enter 发送,Shift+Enter 换行"
            rows="1"
            class="input-textarea"
          ></textarea>
          <div class="input-actions">
            <button class="attach-btn" title="上传文件">
              <label>
                <input type="file" @change="onFile" style="display:none" />
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
              </label>
            </button>
            <button class="send-btn" :disabled="loading || !input.trim()" @click="send">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '../stores/config.js'
import * as chatApi from '../api/chat.js'
import ChartView from '../components/ChartView.vue'
import DataTable from '../components/DataTable.vue'
import ErrorBanner from '../components/ErrorBanner.vue'
import MarkdownContent from '../components/MarkdownContent.vue'
import AgentTrace from '../components/AgentTrace.vue'

const router = useRouter()
const store = useConfigStore()
const messages = ref([])
const input = ref('')
const loading = ref(false)
const files = ref([])
const msgsEl = ref(null)
const sessions = ref([])
const sidebarCollapsed = ref(false)
const inputFocused = ref(false)
const textareaRef = ref(null)
// 流式输出是否已开始（开始后隐藏打字指示器，避免与流式文本重叠）
const streamingActive = ref(false)
const sendingSessionId = ref(null)  // 发送中的会话：切换会话后隐藏加载动画
// 发送中未落库的本地消息（sessionId -> 消息数组）：切走再切回不丢用户气泡
const pendingMsgs = ref(new Map())
let msgSeq = 0
const mkMsg = (role, text = '') => ({
  role, text, timestamp: new Date().toISOString(), _key: `m${++msgSeq}`,
  trace: role === 'assistant' ? [] : undefined
})

// 会话加载竞态保护：快速切换会话时，只应用最后一次请求的结果，
// 避免慢响应覆盖新会话的消息（串话）。
let loadSeq = 0

// 解析图表配置（兼容字符串和对象）
function parseChartConfig(raw) {
  if (!raw) return null
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw)
    } catch (e) {
      return null
    }
  }
  return raw
}

onMounted(async () => {
  if (!store.llmList.length) await store.loadAll()
  await refreshSessions()
  // 若有历史 sessionId，自动加载该会话消息
  if (store.sessionId) {
    await loadSession(store.sessionId, /* silent */ true)
  }
})

const scroll = () => nextTick(() => { if (msgsEl.value) msgsEl.value.scrollTop = msgsEl.value.scrollHeight })

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const diff = now - d
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`
  return `${d.getMonth()+1}/${d.getDate()}`
}

async function refreshSessions() {
  try { sessions.value = await chatApi.listSessions() } catch (_) { sessions.value = [] }
}

async function loadSession(sid, silent = false) {
  if (sid === store.sessionId && messages.value.length > 0 && !silent) return
  const mySeq = ++loadSeq
  store.setSession(sid)
  try {
    const data = await chatApi.getSession(sid)
    if (mySeq !== loadSeq) return  // 已有更新的加载请求，丢弃本次结果
    const traces = data.traces || []
    let traceIdx = 0
    messages.value = (data.messages || []).map(m => ({
      role: m.role,
      text: m.text || '',
      sql: m.sql || null,
      tables: m.tables || (m.table ? [m.table] : []),
      visuals: (m.visuals || (m.chart ? [{ chart: m.chart, image: m.imageBase64 }] : []))
        .map(v => ({ chart: parseChartConfig(v.chart), image: v.image || null })),
      // 轨迹按轮次与 assistant 消息一一对应（后端每轮都落一条，可能为空数组）
      trace: m.role === 'assistant' ? (traces[traceIdx++] || []) : undefined,
      timestamp: m.timestamp || new Date().toISOString()
    }))
    // 合并发送中未落库的本地消息（用户气泡不因切会话丢失）
    const pending = pendingMsgs.value.get(sid) || []
    if (pending.length) messages.value = messages.value.concat(pending)
    scroll()
  } catch (err) {
    if (mySeq !== loadSeq) return
    // 会话后端还不存在（发送中跳走又回来）：展示本地 pending 消息，避免"会话消失"
    const pending = pendingMsgs.value.get(sid) || []
    if (pending.length) {
      messages.value = [...pending]
      scroll()
      return
    }
    if (!silent) messages.value.push({ role: 'assistant', error: err, timestamp: new Date().toISOString() })
  }
}

async function removeSession(sid) {
  if (!confirm('确定删除该会话？此操作不可撤销。')) return
  try {
    await chatApi.deleteSession(sid)
    if (sid === store.sessionId) {
      messages.value = []
      store.newSession()
    }
    await refreshSessions()
  } catch (err) { messages.value.push({ role: 'assistant', error: err }) }
}

// ── 会话重命名（内联编辑） ──────────────────────────────────────────────
const editingId = ref(null)
const editTitle = ref('')
const renameInputEl = ref(null)

function startRename(s) {
  editingId.value = s.id
  editTitle.value = s.title
  nextTick(() => renameInputEl.value?.focus())
}

async function saveRename(s) {
  if (editingId.value !== s.id) return  // 已退出编辑态（如 ESC 或已保存）
  const title = editTitle.value.trim()
  editingId.value = null
  if (!title || title === s.title) return
  try {
    await chatApi.renameSession(s.id, title)
    s.title = title  // 本地立即生效
  } catch (err) {
    messages.value.push({ role: 'assistant', error: err })
  }
}

function cancelRename() {
  editingId.value = null
}

async function onFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  try {
    const res = await chatApi.upload(file)
    files.value.push({ file_id: res.file_id, name: file.name })
  } catch (err) {
    messages.value.push({ role: 'assistant', error: err }); scroll()
  } finally { e.target.value = '' }
}

function removeFile(file_id) {
  files.value = files.value.filter(f => f.file_id !== file_id)
}

async function send() {
  const text = input.value.trim()
  if (!text || loading.value) return
  if (!store.ready) { router.push('/config'); return }
  const sidAtSend = store.sessionId  // 记录发送时的会话，防止响应回来时已切换会话
  // Bug2 修复：发消息立即乐观更新会话列表（时间=现在并置顶），
  // 回复完成后 refreshSessions 会用后端真实 updated_at 校准
  const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 19)
  const curSession = sessions.value.find((x) => x.id === store.sessionId)
  if (curSession) {
    curSession.updated_at = nowLocal
    sessions.value.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
  }
  input.value = ''; loading.value = true; scroll()
  sendingSessionId.value = store.sessionId
  // 重置textarea高度
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto'
  }
  // 用户消息 + 预创建 assistant 占位（带 _key 身份），并缓存到本地（切会话不丢）
  const userMsg = mkMsg('user', text)
  const assistantMsg = mkMsg('assistant', '')
  messages.value.push(userMsg, assistantMsg)
  const pending = pendingMsgs.value.get(store.sessionId) || []
  pending.push(userMsg, assistantMsg)
  pendingMsgs.value.set(store.sessionId, pending)
  streamingActive.value = false
  let streamError = false
  try {
    await chatApi.streamChat(
      {
        message: text,
        session_id: store.sessionId,
        llm_config_id: store.currentLlmId,
        db_config_id: store.currentDbId,
        file_ids: files.value.map((f) => f.file_id)
      },
      {
        onDelta: (t) => {
          if (store.sessionId !== sidAtSend) return
          const target = messages.value.find(m => m._key === assistantMsg._key)
          if (!target) return
          streamingActive.value = true
          target.text += t
        },
        onTrace: (entries) => {
          if (store.sessionId !== sidAtSend) return
          const target = messages.value.find(m => m._key === assistantMsg._key)
          if (!target) return
          target.trace = (target.trace || []).concat(entries)
        },
        onPlan: (plan) => {
          if (store.sessionId !== sidAtSend) return
          const target = messages.value.find(m => m._key === assistantMsg._key)
          if (target) target.plan = plan
        },
        onDone: (res) => {
          // 发送期间跳走了（如去配置中心）：也把会话 id 固化到 store，
          // 回来时按真实 id 加载（此时后端已完成 persist）；用户已切到其他
          // 会话则不覆盖（store.sessionId 已变）
          if (res.session_id && store.sessionId === sidAtSend) store.setSession(res.session_id)
          if (store.sessionId !== sidAtSend) return  // 已切换会话：不显示，消息已落库
          const msg = messages.value.find(m => m._key === assistantMsg._key)
          if (!msg) return
          msg.plan = res.plan || msg.plan || null
          msg.trace = res.trace || msg.trace || []
          msg.sql = res.sql || null
          // 一轮可能有多张图表/表格（visuals/tables 数组），兼容旧字段
          msg.tables = res.tables || (res.table ? [res.table] : [])
          msg.visuals = (res.visuals || (res.chart ? [{ chart: res.chart }] : []))
            .map(v => ({ chart: parseChartConfig(v.chart), image: v.image || null }))
          // 清理回复文本中可能残留的 base64 标记
          if (msg.text && msg.text.includes('<!--IMAGE_BASE64:')) {
            msg.text = msg.text.replace(/<!--IMAGE_BASE64:.*?-->/g, '').trim()
          }
          if (!msg.text.trim()) msg.text = res.reply || '(无文本回复)'
        },
        onError: (err) => {
          streamError = true
          if (store.sessionId === sidAtSend) {
            const target = messages.value.find(m => m._key === assistantMsg._key)
            if (target) target.error = err
          }
        }
      }
    )
    // 后端在 done 前已完成持久化；重新加载一次当前会话，确保大响应、
    // 代理截断或 SSE done 丢失时，最终消息也能立即进入当前视图。
    pendingMsgs.value.delete(sidAtSend)
    if (!streamError && store.sessionId === sidAtSend) {
      await loadSession(sidAtSend, true)
    }
    // 发送成功后刷新会话列表(标题/时间可能更新)
    await refreshSessions()
  } catch (err) {
    if (store.sessionId === sidAtSend) {
      const target = messages.value.find(m => m._key === assistantMsg._key)
      if (target) target.error = err
    }
  } finally { loading.value = false; streamingActive.value = false; sendingSessionId.value = null; scroll() }
}

async function newChat() {
  loadSeq++  // 使任何挂起的 loadSession 失效，避免旧响应覆盖空会话
  if (store.sessionId) {
    try { await chatApi.clearChat(store.sessionId) } catch (_) { /* 忽略清除失败 */ }
  }
  store.newSession(); messages.value = []; files.value = []
}

async function showSchema() {
  if (!store.currentDbId) { router.push('/config'); return }
  try {
    const res = await chatApi.schema(store.currentDbId)
    const text = (res.tables || []).map((t) => `• ${t.name}${t.comment ? '  -- ' + t.comment : ''}`).join('\n') || '(无表)'
    messages.value.push({ role: 'assistant', text: '当前库表结构:\n' + text, timestamp: new Date().toISOString() })
  } catch (err) { messages.value.push({ role: 'assistant', error: err, timestamp: new Date().toISOString() }) }
  scroll()
}

function autoResize() {
  if (!textareaRef.value) return
  textareaRef.value.style.height = 'auto'
  textareaRef.value.style.height = Math.min(textareaRef.value.scrollHeight, 200) + 'px'
}
</script>

<style scoped>
/* ── Layout ── */
.chat-wrap { display: flex; height: calc(100vh - 96px); gap: 0; }

/* ── Sidebar - 柔和背景差异 ── */
.sidebar {
  width: 260px; flex: none;
  background: #FAFAFA; /* 与主区白色形成柔和对比 */
  display: flex; flex-direction: column;
  border-right: 1px solid var(--border-light);
  overflow: hidden;
  transition: width 0.25s ease, opacity 0.25s ease;
}
.sidebar.collapsed { width: 0; overflow: hidden; }
.sidebar-header { display: flex; align-items: center; gap: 8px; padding: 12px; }

.new-btn {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 16px;
  background: var(--brand-gradient);
  color: #fff; border: none;
  border-radius: var(--radius-xl);
  font-size: 13px; font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
.new-btn:hover { box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28); transform: translateY(-1px); }
.new-btn:active { transform: translateY(0); }

.collapse-btn {
  display: flex; align-items: center; justify-content: center;
  width: 32px; height: 32px;
  background: none; border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--text-tertiary); cursor: pointer;
  transition: var(--transition);
}
.collapse-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }

.expand-btn {
  position: fixed; left: 8px; top: 50%; transform: translateY(-50%);
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-primary); border: 1px solid var(--border-color);
  border-radius: 50%; cursor: pointer; z-index: 10;
  color: var(--text-tertiary);
  box-shadow: var(--shadow-sm);
  transition: var(--transition);
}
.expand-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }

.sess-list { flex: 1; overflow-y: auto; padding: 4px 8px 12px; }
.sess-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: var(--radius-sm);
  cursor: pointer; position: relative;
  transition: var(--transition);
  margin-bottom: 2px;
}
.sess-item:hover { background: var(--bg-tertiary); }
.sess-item.active { background: rgba(0, 0, 0, 0.06); }
.sess-item.active .sess-title { color: var(--brand-start); }
.sess-icon { flex: none; color: var(--text-tertiary); }
.sess-content { flex: 1; min-width: 0; }
.sess-title {
  font-size: 13px; font-weight: 500; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-meta { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }

/* 会话项操作按钮（重命名/删除）：hover 渐显 */
.op-btn {
  flex: none;
  display: flex; align-items: center; justify-content: center;
  width: 26px; height: 28px;
  background: none; border: none;
  color: var(--text-tertiary); cursor: pointer;
  border-radius: 6px;
  opacity: 0; transition: var(--transition);
}
.op-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.sess-item:hover .op-btn,
.sess-item:hover .del-btn { opacity: 1; }

.del-btn {
  flex: none;
  display: flex; align-items: center; justify-content: center;
  width: 28px; height: 28px;
  background: none; border: none;
  color: var(--text-tertiary); cursor: pointer;
  border-radius: 6px;
  opacity: 0; transition: var(--transition);
}
.del-btn:hover { background: rgba(239, 68, 68, 0.08); color: #EF4444; }

/* 重命名内联编辑框 */
.sess-edit { padding: 1px 0; }
.sess-edit-input {
  width: 100%;
  padding: 3px 8px;
  font-size: 13px;
  font-family: var(--font-stack);
  color: var(--text-primary);
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  outline: none;
  transition: var(--transition);
}
.sess-edit-input:focus { border-color: var(--brand-start); box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.06); }

.sess-empty {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  color: var(--text-tertiary); font-size: 13px;
  margin-top: 40px;
}

/* ── Main Chat Area ── */
.chat {
  display: flex; flex-direction: column; flex: 1;
  overflow: hidden; padding: 0 32px;
  width: 100%;
  /* 通栏布局：占满右侧剩余空间（原 max-width:900 居中留白已去掉） */
}

/* ── Toolbar ── */
.bar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
  padding: 12px 0; margin-bottom: 8px;
  border-bottom: 1px solid #F0F0F0; /* 更柔和的分割线 */
}
.bar-selects { display: flex; gap: 12px; align-items: center; }
.select-label { display: flex; align-items: center; gap: 6px; }
.select-label-text { font-size: 12px; color: var(--text-tertiary); font-weight: 500; }
.bar-select {
  padding: 6px 28px 6px 10px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 13px; background: var(--bg-primary);
  color: var(--text-primary);
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2386909C' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
  transition: var(--transition);
}
.bar-select:hover { border-color: var(--brand-start); }
.bar-select:focus { outline: none; border-color: var(--brand-start); box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.08); }

.bar-actions { display: flex; gap: 8px; align-items: center; }
.ghost {
  display: flex; align-items: center; gap: 5px;
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 6px 12px; cursor: pointer;
  font-size: 12px; color: var(--text-secondary);
  transition: var(--transition);
}
.ghost:hover { border-color: var(--brand-start); color: var(--brand-start); }
.warnlink {
  display: flex; align-items: center; gap: 4px;
  color: #EF4444; font-size: 12px; font-weight: 600;
  text-decoration: none; padding: 6px 12px;
  background: rgba(239, 68, 68, 0.06);
  border-radius: var(--radius-sm);
  transition: var(--transition);
}
.warnlink:hover { background: rgba(239, 68, 68, 0.1); }

/* ── Upload ── */
.upload { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.upload-btn {
  display: flex; align-items: center; gap: 5px;
  padding: 6px 12px; border: 1px dashed var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 12px; color: var(--text-tertiary);
  cursor: pointer; transition: var(--transition);
}
.upload-btn:hover { border-color: var(--brand-start); color: var(--brand-start); }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(0, 0, 0, 0.05);
  color: var(--brand-start);
  border-radius: var(--radius-xl);
  padding: 4px 10px 4px 10px;
  font-size: 12px;
  position: relative;
}

.chip-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  background: none;
  border: none;
  color: var(--brand-start);
  cursor: pointer;
  border-radius: 50%;
  opacity: 0.6;
  transition: var(--transition);
  margin-left: -2px;
}

.chip-close:hover {
  opacity: 1;
  background: rgba(0, 0, 0, 0.12);
}

/* ── Messages Area ── */
.msgs { 
  flex: 1; 
  overflow-y: auto; 
  padding: 24px 0 8px 0;
  /* 自定义滚动条 */
}
.msgs::-webkit-scrollbar { width: 6px; }
.msgs::-webkit-scrollbar-track { background: transparent; }
.msgs::-webkit-scrollbar-thumb { background: #E5E6EB; border-radius: 3px; }
.msgs::-webkit-scrollbar-thumb:hover { background: #C0C2C8; }

/* ── Empty State ── */
.empty { 
  display: flex; 
  flex-direction: column; 
  align-items: center; 
  gap: 16px; 
  margin-top: 80px; 
}
.empty-icon { 
  opacity: 0.8;
  animation: float 3s ease-in-out infinite;
}
@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-8px); }
}
.empty-text { 
  color: var(--text-tertiary); 
  font-size: 14px; 
  text-align: center; 
  line-height: 1.8;
  max-width: 500px;
}
.empty-text a { 
  color: var(--brand-start); 
  text-decoration: none; 
  font-weight: 500; 
}
.empty-text a:hover { text-decoration: underline; }

/* ── Message Wrapper ── */
.msg-wrapper {
  margin: 24px 0;
  animation: fadeIn 0.3s ease;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.msg-wrapper.user {
  display: flex;
  justify-content: flex-end;
}

.msg-wrapper.assistant {
  display: flex;
  justify-content: flex-start;
}

/* ── Message Layout ── */
.msg { 
  display: flex; 
  gap: 12px; 
  max-width: 800px;
  width: 100%;
  /* 去掉 margin: 0 auto：auto margin 会覆盖 .msg-wrapper.user/.assistant 的
     justify-content，导致用户消息无法靠右、AI 消息无法靠左 */
}

.msg-wrapper.user .msg {
  flex-direction: row;
  justify-content: flex-end; /* 用户消息:气泡+头像整体靠右,头像在最右 */
}

/* ── Avatar -32px圆形 ── */
.avatar {
  width: 32px; 
  height: 32px; 
  flex: none;
  border-radius: 50%;
  display: flex; 
  align-items: center; 
  justify-content: center;
  flex-shrink: 0;
  animation: fadeIn 0.3s ease;
}

.avatar-user {
  background: #F2F3F5;
  color: #4E5969;
}

.avatar-assistant {
  background: var(--brand-gradient);
  color: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

/* ── Message Content ── */
.msg-content { 
  max-width: calc(100% - 44px);
  display: flex;
  flex-direction: column;
}

/* ── Message Bubble ── */
.msg-bubble {
  padding: 14px 16px;
  font-size: 15px;
  line-height: 1.7;
  word-wrap: break-word;
  transition: all 0.2s ease;
}

/* AI消息:极浅背景 */
.msg-bubble.assistant {
  background: #FAFAFA;
  color: var(--text-primary);
  border-radius: 16px 16px 16px 4px;
  padding-left: 4px;
}

/* 用户消息:浅灰背景 */
.msg-bubble.user {
  background: #F2F3F5;
  color: var(--text-primary);
  border-radius: 16px 16px 4px 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.msg-wrapper.user .msg .msg-bubble.user:hover {
  background: #EBECF0;
}

.msg-wrapper.assistant .msg .msg-bubble.assistant:hover {
  background: #F5F6F8;
}

.analysis-plan {
  margin: 10px 0 4px;
  border: 1px solid #E5E6EB;
  border-radius: 9px;
  background: #fff;
  padding: 8px 10px;
  font-size: 12px;
}
.analysis-plan summary { cursor: pointer; font-weight: 600; color: var(--text-secondary); }
.plan-goal { margin: 8px 0; color: var(--text-primary); }
.plan-step { display: flex; gap: 6px; align-items: center; margin: 4px 0; color: var(--text-secondary); }
.plan-status-succeeded { color: #16A34A; }
.plan-status-running { color: #D97706; }
.plan-status-failed { color: #DC2626; }
.plan-warning { margin-top: 7px; color: #B45309; }
.plan-evidence { margin-top: 5px; color: var(--text-tertiary); }

/* ── Text Content ── */
.text {
  white-space: pre-wrap;
}

.text p {
  margin-bottom: 12px;
  line-height: 1.7;
}

.text p:last-child {
  margin-bottom: 0;
}

.text.user {
  color: var(--text-primary);
}

.text.assistant {
  color: var(--text-primary);
}

/* ── SQL Block ── */
.sql { 
  margin: 16px 0 12px 0;
  font-size: 13px;
  border: 1px solid #F0F0F0;
  border-radius: 8px;
  overflow: hidden;
}
.sql summary {
  cursor: pointer; 
  color: var(--text-tertiary);
  font-size: 12px; 
  padding: 8px 12px;
  background: #FAFAFA;
  transition: var(--transition);
  user-select: none;
}
.sql summary:hover { 
  color: var(--brand-start);
  background: #F7F8FA;
}
.sql pre {
  background: #1E1E2E; 
  color: #CDD6F4;
  padding: 14px 16px; 
  border-radius: 0;
  overflow-x: auto; 
  font-family: var(--font-mono);
  font-size: 13px; 
  line-height: 1.6;
  margin: 0;
}

/* DataTable 和 Chart 组件间距 */
[data-v-] :deep(.data-table),
[data-v-] :deep(.chart-view) {
  margin: 16px 0 12px 0;
}

/* ── Timestamp - 外置于气泡下方 ── */
.msg-timestamp {
  font-size: 12px;
  color: #C0C4CC;
  margin-top: 6px;
  padding: 0 4px;
}

.msg-wrapper.user .msg-timestamp {
  text-align: right;
}

.msg-wrapper.assistant .msg-timestamp {
  text-align: left;
}

/* ── Action Buttons - hover渐显 ── */
.msg-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  padding: 0 4px;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.msg-wrapper:hover .msg-actions {
  opacity: 1;
}

.action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background: none;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: all 0.15s ease;
}

.action-btn:hover {
  background: var(--bg-tertiary);
  border-color: var(--border-color);
  color: var(--text-primary);
}

.action-btn:active {
  transform: scale(0.95);
}

/* ── Typing Indicator ── */
.typing-indicator {
  display: flex; 
  align-items: center; 
  gap: 6px;
  padding: 12px 16px;
  background: transparent;
}
.dot {
  width: 7px; 
  height: 7px;
  background: var(--brand-start);
  border-radius: 50%;
  animation: breathing 1.4s ease-in-out infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes breathing {
  0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
  40% { opacity: 1; transform: scale(1); }
}

/* ── Input Area - 底部固定卡片式 ── */
.inputbar { 
  padding: 0 0 16px 0;
  margin-top: 24px;
  border-top: none;
  background: transparent;
}
.input-card {
  display: flex; 
  align-items: flex-end; 
  gap: 8px;
  background: var(--bg-primary);
  border: 1px solid #E5E6EB;
  border-radius: 16px;
  padding: 16px;
  transition: var(--transition);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
}
.input-card.focused {
  border-color: #1F1F1F;
  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.08), 0 4px 12px rgba(0, 0, 0, 0.06);
}

.input-textarea {
  flex: 1; 
  border: none; 
  outline: none; 
  resize: none;
  font-size: 15px; 
  font-family: var(--font-stack);
  color: var(--text-primary);
  line-height: 1.6;
  background: transparent;
  min-height: 56px;
  max-height: 200px;
  overflow-y: auto;
  padding: 8px 0;
}
.input-textarea::placeholder { 
  color: var(--text-tertiary); 
}

.input-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.attach-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  background: none;
  border: none;
  border-radius: 50%;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: var(--transition);
}
.attach-btn:hover {
  background: #F2F3F5;
  color: var(--text-primary);
}
.attach-btn label {
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
}

.send-btn {
  flex: none;
  width: 36px; 
  height: 36px;
  display: flex; 
  align-items: center; 
  justify-content: center;
  background: var(--brand-gradient);
  border: none; 
  border-radius: 50%;
  color: #fff; 
  cursor: pointer;
  transition: var(--transition);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.24);
}
.send-btn:hover:not(:disabled) { 
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.32); 
  transform: translateY(-1px); 
}
.send-btn:disabled { 
  opacity: 0.4; 
  cursor: not-allowed; 
  box-shadow: none; 
}

/* ── Responsive ── */
@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: 56px; bottom: 0; z-index: 50; box-shadow: var(--shadow-lg); }
  .sidebar.collapsed { width: 0; }
  .chat { padding: 0 12px; }
  .msg { max-width: 100%; }
}
</style>
