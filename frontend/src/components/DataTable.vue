<template>
  <section class="data-table" :aria-label="resolvedTitle || '数据表格'">
    <div v-if="resolvedTitle" class="table-header"><h3>{{ resolvedTitle }}</h3></div>
    <div class="table-container" :class="{ 'is-limited': previewLimit > 0 && rowsToShow.length >= previewLimit }">
      <table>
        <thead><tr><th v-for="col in resolvedColumns" :key="col">{{ col }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, idx) in rowsToShow" :key="idx">
            <td v-for="col in resolvedColumns" :key="col" :class="{ numeric: isNumeric(row[col]) }">{{ row[col] }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="hiddenRowCount" class="preview-note">显示 {{ rowsToShow.length }} 行，共 {{ resolvedData.length }} 行</p>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  title: { type: String, default: '' },
  columns: { type: Array, default: null },
  data: { type: Array, default: null },
  table: { type: Object, default: null },
  previewLimit: { type: Number, default: 0 }
})

const resolvedTitle = computed(() => props.title || props.table?.title || '')
const resolvedColumns = computed(() => props.columns || props.table?.columns || [])
const resolvedData = computed(() => {
  if (props.data) return props.data
  const columns = resolvedColumns.value
  const rows = props.table?.rows || []
  return rows.map(row => Array.isArray(row)
    ? Object.fromEntries(columns.map((column, index) => [column, row[index]]))
    : row)
})
const rowsToShow = computed(() => props.previewLimit > 0
  ? resolvedData.value.slice(0, props.previewLimit)
  : resolvedData.value)
const hiddenRowCount = computed(() => Math.max(0, resolvedData.value.length - rowsToShow.value.length))
const isNumeric = value => typeof value === 'number' && Number.isFinite(value)
</script>

<style scoped>
.data-table { width: 100%; color: var(--text-primary); }
.table-header { padding: 0 0 10px; }
.table-header h3 { margin: 0; font-size: 14px; font-weight: 600; }
.table-container { max-height: min(56vh, 520px); overflow: auto; }
.table-container.is-limited { max-height: none; overflow-x: auto; }
table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; }
th, td { padding: 9px 12px; text-align: left; white-space: nowrap; }
th { position: sticky; top: 0; z-index: 1; background: var(--bg-secondary, #f5f3ef); color: var(--text-secondary); font-size: 12px; font-weight: 600; border-bottom: 1px solid var(--border-color); }
td { color: var(--text-primary); }
td.numeric { font-family: var(--font-mono, ui-monospace, monospace); font-variant-numeric: tabular-nums; text-align: right; }
tbody tr:hover { background: var(--bg-tertiary, #f8f6f2); }
.preview-note { margin: 0; padding: 7px 12px; color: var(--text-tertiary); font-size: 11px; }
</style>
