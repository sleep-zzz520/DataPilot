<template>
  <div class="settings-layout">
    <aside class="settings-navigation" aria-label="设置分类">
      <div class="settings-nav-heading">
        <h1>设置</h1>
        <router-link
          to="/chat"
          class="icon-button"
          aria-label="返回分析对话"
          title="返回分析对话"
          ><AppIcon name="back" :size="18"
        /></router-link>
      </div>
      <div class="settings-category-search">
        <AppIcon name="search" :size="16" /><input
          v-model="categoryQuery"
          placeholder="搜索设置"
          aria-label="搜索设置分类"
          @keydown.esc="categoryQuery = ''"
        />
      </div>
      <nav role="tablist" aria-label="设置类别" aria-orientation="vertical">
        <button
          v-for="(category, index) in visibleCategories"
          :key="category.id"
          :id="`settings-tab-${category.id}`"
          type="button"
          role="tab"
          :aria-selected="selectedCategory === category.id"
          :aria-controls="`settings-panel-${category.id}`"
          :tabindex="
            selectedCategory === category.id ||
            (!visibleCategories.some((c) => c.id === selectedCategory) &&
              index === 0)
              ? 0
              : -1
          "
          :class="{ active: selectedCategory === category.id }"
          @click="selectedCategory = category.id"
          @keydown="onCategoryKey($event, index)"
        >
          <AppIcon :name="category.icon" :size="18" /><span>{{
            category.name
          }}</span>
        </button>
      </nav>
      <p v-if="!visibleCategories.length" class="settings-search-empty">
        没有匹配的设置
      </p>
    </aside>
    <main class="settings-content" ref="contentEl">
      <div class="settings-content-inner">
        <header class="settings-content-heading">
          <h2>{{ currentCategory.name }}</h2>
          <button
            v-if="isAdmin && selectedCategory === 'models'"
            class="primary-button"
            @click="
              llmEditing = null;
              llmFormOpen = true;
            "
          >
            <AppIcon name="plus" :size="15" />添加模型</button
          ><button
            v-if="isAdmin && selectedCategory === 'databases'"
            class="primary-button"
            @click="
              dbEditing = null;
              dbFormOpen = true;
            "
          >
            <AppIcon name="plus" :size="15" />添加数据库
          </button>
        </header>
        <div v-if="settingsError" class="settings-error" role="alert">
          {{ settingsError
          }}<button class="text-button" @click="loadSettings">重试</button>
        </div>
        <section
          v-show="selectedCategory === 'account'"
          id="settings-panel-account"
          role="tabpanel"
          aria-labelledby="settings-tab-account"
          aria-label="账户设置"
          tabindex="0"
          class="settings-panel"
        >
          <h3>个人信息</h3>
          <div class="account-setting-row">
            <span>用户名</span
            ><span class="account-setting-name">{{ myName }}</span>
          </div>
          <div class="account-setting-row">
            <span>角色</span><span>{{ isAdmin ? "管理员" : "普通用户" }}</span>
          </div>
          <h3 class="settings-subheading">账户安全</h3>
          <div class="account-setting-row">
            <span>密码</span
            ><button class="text-button" @click="accountActions('password')">
              修改密码
            </button>
          </div>
          <div class="account-setting-row">
            <span>账号</span
            ><button
              class="text-button danger-text"
              @click="accountActions('delete')"
            >
              注销账号
            </button>
          </div>
        </section>
        <section
          v-show="selectedCategory === 'models'"
          id="settings-panel-models"
          role="tabpanel"
          aria-labelledby="settings-tab-models"
          tabindex="0"
          class="settings-panel"
        >
          <ConfigList
            title="已连接的模型"
            :items="store.llmList"
            :current-id="store.currentLlmId"
            :subtitle="
              (it) => [it.model_name, it.base_url].filter(Boolean).join(' · ')
            "
            :readonly="!isAdmin"
            :show-create="false"
            @select="
              (it) => {
                llmEditing = it;
                llmFormOpen = true;
              }
            "
            @set-default="setLlmDefault"
            @remove="removeLlm"
            @rename="renameLlm"
          />
          <div v-if="isAdmin && llmFormOpen" class="settings-editor">
            <h3>{{ llmEditing ? "编辑模型" : "添加模型" }}</h3>
            <LlmConfigForm
              :key="llmEditing?.id || 'new-model'"
              :initial-data="llmEditing"
              @submit="onLlmSubmit"
              @cancel="llmFormOpen = false"
            />
          </div>
        </section>
        <section
          v-show="selectedCategory === 'databases'"
          id="settings-panel-databases"
          role="tabpanel"
          aria-labelledby="settings-tab-databases"
          tabindex="0"
          class="settings-panel"
        >
          <ConfigList
            title="已连接的数据库"
            :items="store.dbList"
            :current-id="store.currentDbId"
            :subtitle="
              (it) =>
                [
                  it.host ? `${it.host}:${it.port || 3306}` : '',
                  it.default_schema || '全部业务库',
                ]
                  .filter(Boolean)
                  .join(' · ')
            "
            :readonly="!isAdmin"
            :show-create="false"
            @select="
              (it) => {
                dbEditing = it;
                dbFormOpen = true;
              }
            "
            @set-default="setDbDefault"
            @remove="removeDb"
            @rename="renameDb"
          />
          <div v-if="isAdmin && dbFormOpen" class="settings-editor">
            <h3>
              {{ dbEditing ? "编辑数据库" : "添加数据库" }}
              <span
                class="settings-help"
                title="建议使用只读数据库账号"
                aria-label="建议使用只读数据库账号"
                >ⓘ</span
              >
            </h3>
            <DbConfigForm
              :key="dbEditing?.id || 'new-db'"
              :initial-data="dbEditing"
              @submit="onDbSubmit"
              @cancel="dbFormOpen = false"
            />
          </div>
        </section>
        <section
          v-if="isAdmin"
          v-show="selectedCategory === 'users'"
          id="settings-panel-users"
          role="tabpanel"
          aria-labelledby="settings-tab-users"
          tabindex="0"
          class="settings-panel"
        >
          <div class="user-list">
            <div v-for="u in users" :key="u.id" class="user-row">
              <div class="user-info">
                <span class="user-name">{{ u.username }}</span
                ><span class="badge">{{
                  u.role === "admin" ? "管理员" : "普通用户"
                }}</span
                ><span v-if="u.username === myName" class="user-self">我</span>
              </div>
              <div class="user-ops">
                <button
                  v-if="u.role === 'user'"
                  class="text-button"
                  @click="setRole(u, 'admin')"
                >
                  提升为管理员</button
                ><button
                  v-else-if="u.username !== myName"
                  class="text-button"
                  @click="setRole(u, 'user')"
                >
                  降为普通用户</button
                ><span v-else class="user-self">当前账号</span>
              </div>
            </div>
            <p v-if="!users.length" class="muted">暂无用户</p>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, inject } from "vue";
import { useConfigStore } from "../stores/config.js";
import * as api from "../api/config.js";
import * as authApi from "../api/auth.js";
import ConfigList from "../components/ConfigList.vue";
import LlmConfigForm from "../components/LlmConfigForm.vue";
import DbConfigForm from "../components/DbConfigForm.vue";
import AppIcon from "../components/AppIcon.vue";

const accountActions = inject("accountActions", () => {});
const store = useConfigStore();
const llmEditing = ref(null);
const dbEditing = ref(null);
// 配置全局共享，仅管理员可修改
const isAdmin = localStorage.getItem("role") === "admin";
const myName = localStorage.getItem("username") || "";

// 用户管理（仅管理员）
const users = ref([]);

const selectedCategory = ref("account");
const categoryQuery = ref("");
const contentEl = ref(null);
const llmFormOpen = ref(false),
  dbFormOpen = ref(false);
const settingsError = ref("");
const categories = computed(() => [
  { id: "account", name: "账户", icon: "user" },
  { id: "models", name: "模型", icon: "brand" },
  { id: "databases", name: "数据库", icon: "database" },
  ...(isAdmin ? [{ id: "users", name: "用户管理", icon: "users" }] : []),
]);
const currentCategory = computed(
  () =>
    categories.value.find((c) => c.id === selectedCategory.value) ||
    categories.value[0],
);
const visibleCategories = computed(() =>
  categories.value.filter((c) => c.name.includes(categoryQuery.value.trim())),
);
watch(selectedCategory, async () => {
  await nextTick();
  if (contentEl.value) contentEl.value.scrollTop = 0;
});
function onCategoryKey(event, index) {
  const keys = [
    "ArrowDown",
    "ArrowUp",
    "ArrowRight",
    "ArrowLeft",
    "Home",
    "End",
  ];
  if (!keys.includes(event.key) || !visibleCategories.value.length) return;
  event.preventDefault();
  const last = visibleCategories.value.length - 1;
  const target =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? last
        : (index +
            (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) +
            last +
            1) %
          (last + 1);
  const tablist = event.currentTarget.parentElement;
  selectedCategory.value = visibleCategories.value[target].id;
  nextTick(() => tablist.querySelectorAll('[role="tab"]')[target]?.focus());
}
async function loadSettings() {
  settingsError.value = "";
  try {
    await store.loadAll();
    await loadUsers();
  } catch (err) {
    settingsError.value = err.suggestion || err.message || "设置加载失败";
  }
}
onMounted(loadSettings);

async function loadUsers() {
  if (!isAdmin) return;
  try {
    users.value = await authApi.listUsers();
  } catch (_) {
    users.value = [];
  }
}

async function setRole(u, role) {
  try {
    await authApi.setUserRole(u.id, role);
    u.role = role;
  } catch (err) {
    alert("操作失败：" + (err.suggestion || err.message));
  }
}

// 保存后：用返回的 view 更新编辑态（新建→变成编辑态，避免重复新建），并刷新列表
async function onLlmSubmit(form) {
  try {
    // 合并编辑态保留字段：默认标记（表单没有该字段，编辑默认项时不能丢）
    const payload = {
      ...form,
      is_default: llmEditing.value?.is_default || false,
    };
    const view = await api.saveLlm(payload);
    llmEditing.value = view;
    await store.loadAll();
    // 保存后测试连接（后端对掩码 key 会取真实 key）
    try {
      await api.testLlm({ ...view, api_key: view.api_key_masked || "" });
      alert("✅ 配置已保存，连接测试通过");
    } catch (err) {
      alert(
        "配置已保存，但连接测试失败：\n" +
          (err.suggestion || err.message || JSON.stringify(err)),
      );
    }
  } catch (err) {
    alert(
      "保存失败：" + (err.suggestion || err.message || JSON.stringify(err)),
    );
  }
}

async function onDbSubmit(form) {
  try {
    // 合并编辑态保留字段：charset / default_schema（表单没有这些字段）
    const payload = {
      ...form,
      charset: dbEditing.value?.charset || "utf8mb4",
      default_schema: dbEditing.value?.default_schema ?? null,
    };
    const view = await api.saveDb(payload);
    dbEditing.value = view;
    await store.loadAll();
    try {
      await api.testDb({ ...view, password: view.password_masked || "" });
      alert("✅ 配置已保存，连接测试通过");
    } catch (err) {
      alert(
        "配置已保存，但连接测试失败：\n" +
          (err.suggestion || err.message || JSON.stringify(err)),
      );
    }
  } catch (err) {
    alert(
      "保存失败：" + (err.suggestion || err.message || JSON.stringify(err)),
    );
  }
}

// 设默认：复用 save 接口，带脱敏 key/密码，后端 upsert 时保留原密文、只改 is_default
async function setLlmDefault(it) {
  await api.saveLlm({ ...it, is_default: true });
  await store.loadAll();
}
async function setDbDefault(it) {
  await api.saveDb({ ...it, is_default: true });
  await store.loadAll();
}

async function removeLlm(it) {
  await api.removeLlm(it.id);
  if (llmEditing.value?.id === it.id) llmEditing.value = null;
  await store.loadAll();
}
async function removeDb(it) {
  await api.removeDb(it.id);
  if (dbEditing.value?.id === it.id) dbEditing.value = null;
  await store.loadAll();
}

// 重命名：复用 save 接口（带脱敏 key/密码，后端保留原密文，只改 name）
async function renameLlm(it, name) {
  try {
    await api.saveLlm({ ...it, name });
    await store.loadAll();
  } catch (err) {
    alert("重命名失败：" + (err.suggestion || err.message));
  }
}
async function renameDb(it, name) {
  try {
    await api.saveDb({ ...it, name });
    await store.loadAll();
  } catch (err) {
    alert("重命名失败：" + (err.suggestion || err.message));
  }
}
</script>

<style scoped>
.settings-layout {
  height: 100%;
  min-height: 0;
  display: grid;
  grid-template-columns: 235px minmax(0, 1fr);
  background: var(--bg-secondary);
}
.settings-navigation {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 33px 18px 24px;
  border-right: 1px solid var(--border-color);
  background: #f0efe9;
  overflow-y: auto;
}
.settings-nav-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 9px;
  margin-bottom: 28px;
}
.settings-nav-heading h1 {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.5px;
}
.settings-category-search {
  display: flex;
  align-items: center;
  gap: 8px;
  border-radius: 10px;
  background: #e7e5de;
  padding: 9px 11px;
  margin: 0 4px 24px;
  color: var(--text-tertiary);
}
.settings-category-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  background: none;
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}
.settings-category-search:focus-within {
  outline: 2px solid #9c4e3555;
  outline-offset: 2px;
}
.settings-navigation nav {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.settings-navigation nav button {
  display: flex;
  align-items: center;
  gap: 11px;
  border: 0;
  border-radius: 10px;
  background: none;
  padding: 12px;
  color: var(--text-secondary);
  font-size: 13px;
  text-align: left;
  transition:
    color 0.18s ease,
    background-color 0.18s ease;
}
.settings-navigation nav button.active {
  background: #e0ddd4;
  color: var(--text-primary);
  font-weight: 500;
}
.settings-navigation nav button:hover {
  color: var(--text-primary);
}
.settings-search-empty {
  padding: 0 12px;
  font-size: 12px;
  color: var(--text-tertiary);
}
.settings-content {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  scrollbar-gutter: stable;
  padding: 48px clamp(28px, 6vw, 100px);
}
.settings-content-inner {
  max-width: 880px;
  margin: 0 auto;
}
.settings-content-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  min-height: 40px;
  margin-bottom: 43px;
}
.settings-content-heading h2 {
  font-size: 29px;
  font-weight: 550;
  letter-spacing: -0.8px;
}
.settings-panel {
  animation: settings-enter 0.2s ease;
}
.settings-panel h3 {
  margin: 0 0 14px;
  font-size: 13px;
  font-weight: 550;
  color: var(--text-primary);
}
.settings-panel .settings-subheading {
  margin-top: 43px;
}
.account-setting-row {
  min-height: 60px;
  padding: 16px 0;
  border-bottom: 1px solid var(--border-light);
}
.account-setting-row + .account-setting-row {
  border-top: 0;
}
.settings-editor {
  margin-top: 35px;
  padding-top: 27px;
  border-top: 1px solid var(--border-color);
}
.settings-help {
  color: var(--text-tertiary);
  font-size: 13px;
  margin-left: 4px;
  cursor: help;
}
.settings-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  font-size: 12px;
  color: #a34434;
  margin-bottom: 24px;
}
.settings-content :deep(.list),
.settings-content :deep(.llm-config-form),
.settings-content :deep(.db-config-form) {
  background: transparent;
  box-shadow: none;
  border: 0;
  border-radius: 0;
  padding: 0;
}
.settings-content :deep(.list .head) {
  margin-bottom: 14px;
}
.settings-content :deep(.list .title) {
  font-size: 13px;
  font-weight: 550;
}
.settings-content :deep(.list .empty) {
  padding: 22px 0;
  text-align: left;
  font-size: 12px;
}
.settings-content :deep(.list li) {
  padding: 16px 12px;
  border-radius: 9px;
}
.settings-content :deep(.list .op-btn:hover) {
  background: transparent;
}
.settings-content :deep(.list .badge) {
  border: 0;
  background: #eae5d9;
  color: #786549;
}
.settings-content :deep(.submit) {
  box-shadow: none;
}
.settings-content :deep(.field input),
.settings-content :deep(.field select) {
  background: var(--bg-primary);
}
.user-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  border-bottom: 1px solid var(--border-light);
  padding: 18px 0;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex-wrap: wrap;
}
.user-name {
  font-size: 13px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.badge,
.user-self {
  font-size: 11px;
  color: var(--text-tertiary);
}
.user-ops {
  flex-shrink: 0;
}
@keyframes settings-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@media (max-width: 760px) {
  .settings-layout {
    grid-template-columns: 170px minmax(0, 1fr);
  }
  .settings-navigation {
    padding: 24px 10px;
  }
  .settings-category-search {
    padding: 8px;
    margin-left: 0;
    margin-right: 0;
  }
  .settings-content {
    padding: 28px 22px;
  }
  .settings-content-heading {
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 28px;
  }
  .settings-content-heading h2 {
    font-size: 24px;
  }
  .settings-content-heading .primary-button {
    font-size: 11px;
    padding: 8px 10px;
  }
}
@media (max-width: 540px) {
  .settings-layout {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }
  .settings-navigation {
    padding: 18px 16px 13px;
    border-right: 0;
    border-bottom: 1px solid var(--border-color);
    overflow: visible;
  }
  .settings-nav-heading {
    padding: 0 2px;
    margin-bottom: 15px;
  }
  .settings-nav-heading h1 {
    font-size: 19px;
  }
  .settings-category-search {
    display: none;
  }
  .settings-navigation nav {
    flex-direction: row;
    gap: 4px;
    overflow-x: auto;
  }
  .settings-navigation nav button {
    padding: 9px 11px;
    font-size: 12px;
    flex-shrink: 0;
    gap: 7px;
  }
  .settings-navigation nav button svg {
    width: 15px;
    height: 15px;
  }
  .settings-content {
    padding: 26px 20px;
  }
  .user-row {
    align-items: flex-start;
  }
  .user-info {
    gap: 6px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .settings-panel {
    animation: none;
  }
}
</style>
