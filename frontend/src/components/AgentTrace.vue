<template>
  <section v-if="entries.length" class="trace" :class="{ compact }">
    <button v-if="compact" class="trace-head trace-toggle" type="button" :aria-expanded="traceOpen" @click="toggleTrace">
      <span class="trace-title">Agent 调用轨迹</span>
      <span class="trace-count">{{ entries.length }} 步</span>
      <svg class="trace-chevron" :class="{ open: traceOpen }" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <div v-else class="trace-head">
      <span class="trace-title">Agent 调用轨迹</span>
      <span class="trace-count">{{ entries.length }} 步</span>
    </div>
    <div v-if="!compact || traceOpen" class="trace-list">
      <div v-for="e in entries" :key="e.seq" class="trace-step" :class="['depth-' + Math.min(e.depth || 0, 5), e.status]" @click="toggle(e.seq)">
        <div class="trace-main" role="button" tabindex="0" :aria-expanded="expanded.has(e.seq)" @keydown.enter.prevent.stop="toggle(e.seq)" @keydown.space.prevent.stop="toggle(e.seq)">
          <span class="trace-dot" :class="e.status"></span>
          <span class="trace-agent" :class="agentClass(e.agent)">{{ agentLabel(e.agent) }}</span>
          <span class="trace-tool">{{ e.tool }}</span>
          <span class="trace-status" :class="e.status">{{ statusLabel(e.status) }}</span>
          <span v-if="e.duration_ms != null" class="trace-dur">{{ e.duration_ms }}ms</span>
          <svg class="step-chevron" :class="{ open: expanded.has(e.seq) }" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </div>
        <div v-if="expanded.has(e.seq)" class="trace-detail">
          <div class="trace-field"><span class="trace-label">入参</span><pre class="trace-code">{{ e.input || '(空)' }}</pre></div>
          <div class="trace-field"><span class="trace-label">结果</span><pre class="trace-code">{{ e.output || (e.status === 'running' ? '执行中…' : '(空)') }}</pre></div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  entries: { type: Array, default: () => [] },
  compact: { type: Boolean, default: false }
})

const expanded = ref(new Set())
const traceOpen = ref(false)
let traceOpenManuallySet = false

watch(() => props.entries.some(entry => entry.status === 'running'), running => {
  if (!traceOpenManuallySet) traceOpen.value = running
}, { immediate: true })

function toggle(seq) {
  const next = new Set(expanded.value)
  next.has(seq) ? next.delete(seq) : next.add(seq)
  expanded.value = next
}

function toggleTrace() {
  traceOpenManuallySet = true
  traceOpen.value = !traceOpen.value
}

const AGENT_LABELS = {
  supervisor: '主管', analysis_planner: '分析规划', data_executor: '数据执行',
  statistical_validator: '统计验证', insight_writer: '洞察表达', agent: 'Agent'
}
function agentLabel(name) { return AGENT_LABELS[name] || name || 'Agent' }
function agentClass(name) { return 'agent-' + (AGENT_LABELS[name] ? name : 'other') }
function statusLabel(status) { return { ok: '成功', error: '失败', running: '执行中' }[status] || status }
</script>

<style scoped>
.trace { margin: 12px 0 4px; color: var(--text-primary); }
.trace-head { display: flex; width: 100%; align-items: center; gap: 8px; padding: 7px 0; color: var(--text-secondary); font: inherit; font-size: 12px; font-weight: 600; text-align: left; }
.trace-toggle { border: 0; background: transparent; cursor: pointer; }
.trace-toggle:hover { color: var(--text-primary); }
.trace-title { min-width: 0; }
.trace-count { margin-left: auto; color: var(--text-tertiary); font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }
.trace-chevron, .step-chevron { flex: none; color: var(--text-tertiary); transition: transform .18s ease; }
.trace-chevron.open, .step-chevron.open { transform: rotate(180deg); }
.trace-list { border-top: 1px solid var(--border-color); }
.trace-step { border-bottom: 1px solid var(--border-color); }
.trace-main { display: flex; align-items: center; gap: 8px; min-height: 34px; padding: 5px 4px; font-size: 12px; cursor: pointer; transition: background .15s ease; }
.trace-main:hover { background: var(--bg-tertiary, #f8f6f2); }
.trace-main:focus-visible, .trace-toggle:focus-visible { outline: 2px solid #9a6b43; outline-offset: 2px; border-radius: 4px; }
.trace-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: #aaa49b; }
.trace-dot.ok { background: #71816e; }.trace-dot.error { background: #b65e4e; }.trace-dot.running { background: #b17a3e; }
.trace-agent { flex: none; border-radius: 4px; padding: 2px 6px; background: #efede8; color: #625b51; font-size: 10px; font-weight: 600; }
.trace-agent.agent-supervisor { background: #e8e3dc; color: #403a34; }
.trace-agent.agent-analysis_planner { background: #f4e8da; color: #815b37; }
.trace-agent.agent-data_executor { background: #e8ecee; color: #52636b; }
.trace-agent.agent-statistical_validator { background: #eeeae0; color: #72684f; }
.trace-agent.agent-insight_writer { background: #eee8e2; color: #786456; }
.trace-agent.agent-agent { background: #f2eadb; color: #81683f; }
.trace-tool { overflow: hidden; color: var(--text-primary); font-family: var(--font-mono); text-overflow: ellipsis; white-space: nowrap; }
.trace-status { flex: none; color: var(--text-tertiary); font-size: 11px; }.trace-status.ok { color: #647660; }.trace-status.error { color: #a64f42; }.trace-status.running { color: #986b33; }
.trace-dur { flex: none; color: var(--text-tertiary); font-size: 11px; font-variant-numeric: tabular-nums; }
.step-chevron { margin-left: auto; }
.trace-step.depth-1 .trace-main { padding-left: 20px; }.trace-step.depth-2 .trace-main { padding-left: 36px; }.trace-step.depth-3 .trace-main { padding-left: 52px; }.trace-step.depth-4 .trace-main { padding-left: 68px; }.trace-step.depth-5 .trace-main { padding-left: 84px; }
.trace-detail { display: grid; gap: 7px; padding: 2px 8px 10px 20px; }
.trace-field { display: flex; align-items: flex-start; gap: 10px; }
.trace-label { flex: none; padding-top: 4px; color: var(--text-tertiary); font-size: 11px; }
.trace-code { flex: 1; max-height: 160px; overflow: auto; margin: 0; border-left: 2px solid #e3ded6; padding: 5px 9px; background: var(--bg-secondary, #f5f3ef); color: var(--text-secondary); font-family: var(--font-mono); font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
</style>
