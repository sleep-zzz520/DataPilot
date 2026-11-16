<template>
  <dialog
    :id="id"
    ref="dialogEl"
    class="search-dialog"
    :aria-labelledby="`${id}-title`"
    @cancel.prevent="close"
    @click.self="close"
  >
    <div class="search-dialog-content">
      <header class="search-dialog-heading">
        <h2 :id="`${id}-title`">{{ title }}</h2>
        <button type="button" class="icon-button" aria-label="关闭搜索弹窗" @click="close">
          <AppIcon name="close" :size="20" />
        </button>
      </header>
      <div class="search-dialog-field">
        <AppIcon name="search" :size="18" />
        <input
          v-model="query"
          autofocus
          :placeholder="placeholder"
          :aria-label="placeholder"
          role="combobox"
          aria-autocomplete="list"
          :aria-expanded="open"
          :aria-controls="`${id}-results`"
          :aria-activedescendant="filteredItems[activeIndex] ? `${id}-result-${activeIndex}` : undefined"
          @keydown="onSearchKey"
        />
      </div>
      <div ref="resultsEl" class="search-dialog-results" :aria-busy="busy">
        <ul :id="`${id}-results`" role="listbox" :aria-label="`${title}结果`">
          <li v-for="(item, index) in filteredItems" :key="item.id" role="none">
            <button
              :id="`${id}-result-${index}`"
              type="button"
              role="option"
              tabindex="-1"
              class="search-result"
              :class="{ highlighted: index === activeIndex }"
              :aria-selected="index === activeIndex"
              :title="item.title"
              @mousemove="activeIndex = index"
              @click="select(item)"
            >
              <AppIcon :name="item.icon || 'chat'" :size="17" />
              <span class="search-result-title">{{ item.title }}</span>
              <span v-if="item.active" class="search-result-current">当前</span>
            </button>
          </li>
        </ul>
        <p v-if="!filteredItems.length" class="search-dialog-empty" role="status">
          {{ busy ? "载入中…" : query.trim() ? "没有匹配的结果" : emptyText }}
        </p>
      </div>
    </div>
  </dialog>
</template>

<script setup>
import { computed, nextTick, ref, watch } from "vue";
import AppIcon from "./AppIcon.vue";

const props = defineProps({
  id: { type: String, required: true },
  open: Boolean,
  title: { type: String, default: "搜索" },
  placeholder: { type: String, default: "输入关键词" },
  items: { type: Array, default: () => [] },
  busy: Boolean,
  emptyText: { type: String, default: "暂无可搜索的内容" },
});
const emit = defineEmits(["update:open", "select"]);
const dialogEl = ref(null);
const resultsEl = ref(null);
const query = ref("");
const activeIndex = ref(0);
const filteredItems = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return props.items.filter(item => String(item.title || "").toLocaleLowerCase().includes(term));
});
watch(filteredItems, () => { activeIndex.value = 0; });
watch(() => props.open, async open => {
  if (open) query.value = "";
  activeIndex.value = 0;
  await nextTick();
  const dialog = dialogEl.value;
  if (!dialog) return;
  if (open && !dialog.open) {
    if (resultsEl.value) resultsEl.value.scrollTop = 0;
    dialog.showModal();
  }
  else if (!open && dialog.open) dialog.close();
}, { immediate: true });

function close() {
  emit("update:open", false);
}
function select(item) {
  emit("update:open", false);
  emit("select", item.id);
}
function onSearchKey(event) {
  if (event.isComposing || !filteredItems.value.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const count = filteredItems.value.length;
    activeIndex.value = (activeIndex.value + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
    const option = dialogEl.value?.querySelector(`[id="${props.id}-result-${activeIndex.value}"]`);
    option?.scrollIntoView?.({ block: "nearest" });
  } else if (event.key === "Enter") {
    event.preventDefault();
    const item = filteredItems.value[activeIndex.value];
    if (item) select(item);
  }
}
</script>

<style scoped>
.search-dialog {
  width: min(600px, calc(100vw - 32px));
  max-height: min(620px, calc(100dvh - 40px));
  margin: auto;
  padding: 0;
  border: 1px solid var(--border-color);
  border-radius: 20px;
  background: var(--bg-secondary);
  color: var(--text-primary);
  box-shadow: 0 20px 70px #201e1926;
  overflow: hidden;
  visibility: visible;
  pointer-events: auto;
}
.search-dialog[open] { animation: search-dialog-enter 0.18s ease; }
.search-dialog::backdrop { background: #201e1966; }
.search-dialog-content {
  display: flex;
  flex-direction: column;
  max-height: inherit;
}
.search-dialog-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 22px 24px 17px;
}
.search-dialog-heading h2 { font-size: 19px; font-weight: 550; letter-spacing: -0.3px; }
.search-dialog-field {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  margin: 0 24px 12px;
  padding: 12px 14px;
  border: 1px solid var(--border-color);
  border-radius: 11px;
  color: var(--text-tertiary);
  background: var(--bg-primary);
}
.search-dialog-field:focus-within { border-color: #a69783; }
.search-dialog-field input {
  width: 100%;
  min-width: 0;
  border: 0;
  padding: 0;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: 14px;
}
.search-dialog-results { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 16px 16px; }
.search-dialog-results ul { list-style: none; padding: 0; margin: 0; }
.search-result {
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-secondary);
  padding: 13px 12px;
  text-align: left;
  font: inherit;
  font-size: 13px;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.search-result.highlighted { background: #e7e3da; color: var(--text-primary); }
.search-result svg { flex-shrink: 0; }
.search-result-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-current { margin-left: auto; flex-shrink: 0; color: var(--text-tertiary); font-size: 11px; }
.search-dialog-empty { margin: 0; padding: 34px 12px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
@keyframes search-dialog-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
@media (max-width: 540px) {
  .search-dialog { max-height: calc(100dvh - 24px); border-radius: 16px; }
  .search-dialog-heading { padding: 19px 20px 15px; }
  .search-dialog-field { margin-right: 20px; margin-left: 20px; }
  .search-dialog-results { padding-right: 12px; padding-left: 12px; }
}
@media (prefers-reduced-motion: reduce) {
  .search-dialog[open] { animation: none; }
  .search-result { transition: none; }
}
</style>
