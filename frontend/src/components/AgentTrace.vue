<template>
  <div v-if="entries.length" class="trace">
    <div class="trace-head">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/><path d="M12 2v4m0 12v4M2 12h4m12 0h4"/>
        <path d="M4.9 4.9l2.8 2.8m8.6 8.6l2.8 2.8m0-14.2l-2.8 2.8m-8.6 8.6l-2.8 2.8"/>
      </svg>
      Agent 调用轨迹
      <span class="trace-count">{{ entries.length }} 步</span>
    </div>
    <div class="trace-list">
      <div
        v-for="e in entries"
        :key="e.seq"
        class="trace-step"
        :class="['depth-' + Math.min(e.depth || 0, 5), e.status]"
        @click="toggle(e.seq)"
      >
        <div class="trace-main">
          <span class="trace-dot" :class="e.status"></span>
          <span class="trace-agent" :class="agentClass(e.agent)">{{ agentLabel(e.agent) }}</span>
          <span class="trace-tool">{{ e.tool }}</span>
          <span class="trace-status" :class="e.status">{{ statusLabel(e.status) }}</span>
          <span v-if="e.duration_ms != null" class="trace-dur">{{ e.duration_ms }}ms</span>
          <svg class="trace-chevron" :class="{ open: expanded.has(e.seq) }" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"/>
          </svg>
        </div>
        <div v-if="expanded.has(e.seq)" class="trace-detail">
          <div class="trace-field">
            <span class="trace-label">入参</span>
            <pre class="trace-code">{{ e.input || '(空)' }}</pre>
          </div>
          <div class="trace-field">
            <span class="trace-label">结果</span>
            <pre class="trace-code">{{ e.output || (e.status === 'running' ? '执行中…' : '(空)') }}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  entries: { type: Array, default: () => [] }
})

// 展开态：按 seq 记录，默认折叠（保持气泡紧凑，点击任意一行展开/收起）
const expanded = ref(new Set())
watch(() => props.entries.length, () => { expanded.value = new Set() })

function toggle(seq) {
  const s = new Set(expanded.value)
  s.has(seq) ? s.delete(seq) : s.add(seq)
  expanded.value = s
}

const AGENT_LABELS = {
  supervisor: '主管',
  analysis_planner: '分析规划',
  data_executor: '数据执行',
  statistical_validator: '统计验证',
  insight_writer: '洞察表达',
  agent: 'Agent'
}

function agentLabel(name) {
  return AGENT_LABELS[name] || name || 'Agent'
}

function agentClass(name) {
  return 'agent-' + (AGENT_LABELS[name] ? name : 'other')
}

function statusLabel(s) {
  return { ok: '成功', error: '失败', running: '执行中' }[s] || s
}
</script>

<style scoped>
.trace {
  margin: 12px 0 4px;
  border: 1px solid #ECEDEF;
  border-radius: 10px;
  background: #FCFCFD;
  overflow: hidden;
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  background: #F7F8FA;
  border-bottom: 1px solid #ECEDEF;
}
.trace-head svg { color: var(--brand-start); }
.trace-count {
  margin-left: auto;
  font-weight: 500;
  color: var(--text-tertiary);
  background: #fff;
  border: 1px solid #E5E6EB;
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 11px;
}

.trace-list { padding: 4px 0; }
.trace-step { cursor: pointer; transition: background 0.15s ease; }
.trace-step:hover { background: #F7F8FA; }

.trace-main {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 12.5px;
}
.trace-dot {
  flex: none;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #C0C4CC;
}
.trace-dot.ok { background: #34C759; }
.trace-dot.error { background: #EF4444; }
.trace-dot.running { background: #F59E0B; }

.trace-agent {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  background: #F0F1F3;
  color: var(--text-secondary);
}
.trace-agent.agent-supervisor { background: #1F1F1F; color: #fff; }
.trace-agent.agent-analysis_planner { background: #FFF1E6; color: #C2410C; }
.trace-agent.agent-data_executor { background: #E8F0FE; color: #1A56DB; }
.trace-agent.agent-statistical_validator { background: #E6F7EE; color: #15803D; }
.trace-agent.agent-insight_writer { background: #F3E8FF; color: #9333EA; }
.trace-agent.agent-agent { background: #FEF3C7; color: #B45309; }

.trace-tool {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-status {
  flex: none;
  font-size: 11px;
  color: var(--text-tertiary);
}
.trace-status.ok { color: #34C759; }
.trace-status.error { color: #EF4444; }
.trace-status.running { color: #F59E0B; }
.trace-dur {
  flex: none;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.trace-chevron {
  margin-left: auto;
  flex: none;
  color: var(--text-tertiary);
  transition: transform 0.2s ease;
}
.trace-chevron.open { transform: rotate(180deg); }

/* 层级缩进：专家内部工具比主管入口深一级 */
.trace-step.depth-1 .trace-main { padding-left: 26px; }
.trace-step.depth-2 .trace-main { padding-left: 40px; }
.trace-step.depth-3 .trace-main { padding-left: 54px; }
.trace-step.depth-4 .trace-main { padding-left: 68px; }
.trace-step.depth-5 .trace-main { padding-left: 82px; }

.trace-detail {
  padding: 0 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.trace-step.depth-1 .trace-detail { padding-left: 26px; }
.trace-field {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.trace-label {
  flex: none;
  font-size: 11px;
  color: var(--text-tertiary);
  padding-top: 3px;
}
.trace-code {
  flex: 1;
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  background: #F4F5F7;
  border: 1px solid #ECEDEF;
  border-radius: 6px;
  padding: 6px 8px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow-y: auto;
}
</style>
