<template>
  <aside class="workspace-sidebar" :class="{ collapsed }" aria-label="会话导航">
    <div class="sidebar-brand">
      <span class="brand-mark"><AppIcon name="brand" :size="22" /></span>
      <strong>数据分析 Agent</strong>
      <button
        class="icon-button collapse-control"
        aria-label="收起侧栏"
        @click="$emit('toggle')"
      >
        <AppIcon name="panel" />
      </button>
    </div>
    <div class="sidebar-primary">
      <button
        class="sidebar-action new-conversation"
        @click="$emit('new-chat')"
      >
        <AppIcon name="plus" :size="18" /><span>新建会话</span>
      </button>
      <button
        class="sidebar-action search-conversations"
        :aria-expanded="searchOpen"
        aria-label="搜索会话"
        @click="toggleSearch"
      >
        <AppIcon name="search" :size="18" /><span>搜索会话</span>
      </button>
    </div>
    <Transition name="search">
      <input
        v-if="searchOpen"
        ref="searchEl"
        v-model="query"
        class="session-search"
        placeholder="搜索会话"
        aria-label="搜索会话标题"
        @keydown.esc="closeSearch"
      />
    </Transition>
    <div class="sidebar-scroll">
      <div class="sidebar-section-title recent-section">
        <button
          class="section-toggle"
          :aria-expanded="recentOpen"
          aria-controls="sidebar-recent-list"
          aria-label="展开或收起最近会话"
          @click="recentOpen = !recentOpen"
        >
          <AppIcon
            name="chevron"
            :size="12"
            :class="{ rotated: recentOpen }"
          /><span>最近会话</span>
        </button>
        <span v-if="busy" class="muted">载入中</span>
      </div>
      <Transition name="sidebar-tree">
        <div v-if="recentOpen" id="sidebar-recent-list">
          <button
            v-for="session in visibleSessions"
            :key="session.id"
            class="session-link ungrouped-session"
            :class="{ active: session.id === sessionId }"
            :aria-current="session.id === sessionId ? 'page' : undefined"
            :title="session.title || '新会话'"
            @click="$emit('select-session', session.id)"
          >
            <span>{{ session.title || "新会话" }}</span>
          </button>
          <div v-if="!visibleSessions.length && !busy" class="sidebar-empty">
            {{ query.trim() ? "没有匹配的会话" : "暂无会话" }}
          </div>
        </div>
      </Transition>
    </div>
    <div class="sidebar-footer">
      <details class="account-menu">
        <summary>
          <span class="account-avatar">{{
            username.slice(0, 1).toUpperCase() || "我"
          }}</span
          ><span>{{ username || "账户" }}</span
          ><AppIcon name="more" />
        </summary>
        <div class="account-popover">
          <span>{{ role === "admin" ? "管理员" : "普通用户" }}</span
          ><router-link to="/config" class="account-settings"
            ><AppIcon name="settings" :size="15" />设置</router-link
          ><button class="account-logout" @click="$emit('account', 'logout')">
            <AppIcon name="logout" :size="18" />退出登录
          </button>
        </div>
      </details>
    </div>
  </aside>
</template>
<script setup>
import { computed, nextTick, ref } from "vue";
import AppIcon from "./AppIcon.vue";
const props = defineProps({
  sessions: { type: Array, default: () => [] },
  sessionId: String,
  collapsed: Boolean,
  busy: Boolean,
  username: {
    type: String,
    default: () => localStorage.getItem("username") || "",
  },
  role: { type: String, default: () => localStorage.getItem("role") || "user" },
});
defineEmits(["toggle", "new-chat", "select-session", "account"]);
const searchOpen = ref(false),
  query = ref(""),
  recentOpen = ref(true),
  searchEl = ref(null);
const visibleSessions = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return props.sessions.filter((session) =>
    String(session.title || "新会话")
      .toLocaleLowerCase()
      .includes(term),
  );
});
function closeSearch() {
  query.value = "";
  searchOpen.value = false;
}
async function toggleSearch() {
  if (searchOpen.value) {
    closeSearch();
    return;
  }
  searchOpen.value = true;
  recentOpen.value = true;
  await nextTick();
  searchEl.value?.focus();
}
</script>
