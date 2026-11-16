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
        aria-haspopup="dialog"
        aria-label="搜索会话"
        @click="searchOpen = true"
      >
        <AppIcon name="search" :size="18" /><span>搜索会话</span>
      </button>
    </div>
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
            v-for="session in sessions"
            :key="session.id"
            class="session-link ungrouped-session"
            :class="{ active: session.id === sessionId }"
            :aria-current="session.id === sessionId ? 'page' : undefined"
            :title="session.title || '新会话'"
            @click="$emit('select-session', session.id)"
          >
            <span>{{ session.title || "新会话" }}</span>
          </button>
          <div v-if="!sessions.length && !busy" class="sidebar-empty">暂无会话</div>
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
    <SearchDialog
      id="session-search"
      v-model:open="searchOpen"
      title="搜索会话"
      placeholder="输入会话标题"
      :items="searchItems"
      :busy="busy"
      empty-text="暂无会话"
      @select="$emit('select-session', $event)"
    />
  </aside>
</template>
<script setup>
import { computed, ref } from "vue";
import AppIcon from "./AppIcon.vue";
import SearchDialog from "./SearchDialog.vue";
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
const searchOpen = ref(false);
const recentOpen = ref(true);
const searchItems = computed(() => props.sessions.map(session => ({
  id: session.id,
  title: session.title || "新会话",
  active: session.id === props.sessionId,
})));
</script>
