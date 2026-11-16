<template>
  <div class="list">
    <div class="head">
      <span class="title">{{ title }}</span>
      <button v-if="!readonly && showCreate" class="new" @click="$emit('create')">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        新建
      </button>
    </div>
    <div v-if="!items.length" class="empty">{{ readonly ? '暂无可用配置。' : '暂无配置，点击添加开始配置。' }}</div>
    <ul v-else>
      <li v-for="it in items" :key="it.id" :class="{ active: it.id === currentId }">
        <div class="info" @click="!readonly && $emit('select', it)">
          <!-- 重命名编辑态 -->
          <input
            v-if="editingId === it.id"
            v-model="editName"
            ref="renameInputEl"
            class="rename-input"
            @click.stop
            @keydown.enter="saveRename(it)"
            @keydown.esc="cancelRename"
            @blur="saveRename(it)"
          />
          <template v-else>
            <span class="name">{{ it.name }}</span>
            <span v-if="it.is_default" class="badge def">默认</span>
            <span v-if="it.id === currentId" class="badge cur">当前</span>
          </template>
          <span class="sub">{{ subtitle(it) }}</span>
        </div>
        <div class="ops" v-if="!readonly">
          <button class="op-btn" @click.stop="startRename(it)" title="重命名">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
          <button v-if="!it.is_default" class="op-btn" @click="$emit('setDefault', it)" title="设为默认">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 10 10c0 5.52-4.48 10-10 10S2 17.52 2 12 6.48 2 12 2z"/><path d="M8 12l2 2 4-4"/></svg>
          </button>
          <button class="op-btn del" @click="onRemove(it)" title="删除">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>
<script setup>
import { ref, nextTick } from 'vue'
defineProps({ items: Array, currentId: [Number, null], title: String, subtitle: { type: Function, default: () => '' }, readonly: Boolean, showCreate: { type: Boolean, default: true } })
const emit = defineEmits(['select','setDefault','remove','create','rename'])
const onRemove = (it) => { if (confirm(`确定删除配置「${it.name}」？`)) emit('remove', it) }

// ── 内联重命名 ─────────────────────────────────────────────────────
const editingId = ref(null)
const editName = ref('')
const renameInputEl = ref(null)

function startRename(it) {
  editingId.value = it.id
  editName.value = it.name
  nextTick(() => renameInputEl.value?.focus())
}
function saveRename(it) {
  if (editingId.value !== it.id) return  // 已退出编辑态
  const name = editName.value.trim()
  editingId.value = null
  if (!name || name === it.name) return
  emit('rename', it, name)
}
function cancelRename() { editingId.value = null }
</script>
<style scoped>
.list {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-primary);
  padding: 12px;
  box-shadow: var(--shadow-sm);
}
.head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.title { font-weight: 700; font-size: 14px; color: var(--text-primary); }
.new {
  display: flex; align-items: center; gap: 4px;
  background: var(--brand-gradient);
  color: #fff; border: 0;
  padding: 6px 12px; border-radius: var(--radius-xl);
  cursor: pointer; font-size: 12px; font-weight: 600;
  transition: var(--transition);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.16);
}
.new:hover { box-shadow: 0 3px 10px rgba(0, 0, 0, 0.24); transform: translateY(-1px); }

.empty { color: var(--text-tertiary); font-size: 13px; padding: 12px; text-align: center; }

ul { list-style: none; margin: 0; padding: 0; }
li {
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap;                /* 空间不足时操作按钮换行，避免挤出区块 */
  padding: 10px 12px; border-radius: var(--radius-sm);
  transition: var(--transition);
}
li:hover { background: var(--bg-tertiary); }
li.active { background: rgba(0, 0, 0, 0.05); }

.info { cursor: pointer; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1 1 auto; min-width: 0; }
.name {
  font-weight: 600; font-size: 13px; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 100%;
}
/* 副标题单独一行，超长截断（防止把操作按钮挤出区块） */
.sub {
  color: var(--text-tertiary); font-size: 12px;
  flex-basis: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rename-input {
  width: 100%;
  padding: 4px 8px;
  font-size: 13px;
  font-family: var(--font-stack);
  color: var(--text-primary);
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  outline: none;
}
.rename-input:focus { border-color: var(--brand-start); box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.06); }

.badge {
  font-size: 11px; padding: 2px 8px; border-radius: var(--radius-xl);
  font-weight: 500;
}
.badge.def { background: rgba(34, 197, 94, 0.1); color: #166534; }
.badge.cur { background: rgba(0, 0, 0, 0.08); color: var(--brand-start); }

.ops { display: flex; gap: 4px; }
.op-btn {
  display: flex; align-items: center; justify-content: center;
  width: 28px; height: 28px;
  background: none; border: 1px solid transparent;
  border-radius: 6px; cursor: pointer;
  color: var(--text-tertiary);
  transition: var(--transition);
}
.op-btn:hover { background: var(--bg-primary); border-color: var(--border-color); color: var(--text-primary); }
.op-btn.del:hover { color: #EF4444; border-color: rgba(239, 68, 68, 0.2); background: rgba(239, 68, 68, 0.04); }
</style>
