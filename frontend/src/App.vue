<template>
  <div class="app" :class="{ 'app-login': $route.name === 'login' }">
    <header
      v-if="$route.name !== 'chat' && $route.path !== '/config'"
      class="topbar"
    >
      <div class="topbar-inner">
        <span class="brand">
          <svg
            class="brand-icon"
            viewBox="0 0 24 24"
            width="22"
            height="22"
            fill="none"
            stroke="url(#grad)"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <defs>
              <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#1F1F1F" />
                <stop offset="100%" stop-color="#444444" />
              </linearGradient>
            </defs>
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5" />
            <path d="M2 12l10 5 10-5" />
          </svg>
          数据分析 Agent
        </span>
        <nav class="nav">
          <router-link to="/chat" class="navlink" active-class="active">
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path
                d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
              />
            </svg>
            分析对话
          </router-link>
          <router-link to="/config" class="navlink" active-class="active">
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="3" />
              <path
                d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"
              />
            </svg>
            设置
          </router-link>
        </nav>
        <details v-if="username" class="account-menu settings-account">
          <summary>
            <span class="account-avatar">{{
              username.slice(0, 1).toUpperCase()
            }}</span
            ><span>{{ username }}</span>
          </summary>
          <div class="account-popover">
            <span>{{ role === "admin" ? "管理员" : "普通用户" }}</span
            ><router-link to="/config" class="account-settings"
              ><AppIcon name="settings" :size="15" />设置</router-link
            ><button class="account-logout" @click="logout">
              <AppIcon name="logout" :size="18" />退出登录
            </button>
          </div>
        </details>
      </div>
    </header>

    <!-- 修改密码弹窗 -->
    <div
      v-if="showPwdModal"
      class="modal-mask"
      @click.self="showPwdModal = false"
    >
      <div class="modal">
        <h3 class="modal-title">修改密码</h3>
        <div class="modal-field">
          <label>原密码</label>
          <input v-model="oldPwd" type="password" placeholder="输入当前密码" />
        </div>
        <div class="modal-field">
          <label>新密码</label>
          <input v-model="newPwd" type="password" placeholder="至少 4 位" />
        </div>
        <p v-if="pwdMsg" class="modal-msg" :class="{ err: pwdErr }">
          {{ pwdMsg }}
        </p>
        <div class="modal-actions">
          <button class="modal-btn ghost" @click="showPwdModal = false">
            取消
          </button>
          <button
            class="modal-btn primary"
            :disabled="pwdLoading"
            @click="doChangePwd"
          >
            {{ pwdLoading ? "修改中..." : "确认修改" }}
          </button>
        </div>
      </div>
    </div>
    <main
      class="page"
      :class="{
        'page-fluid': $route.name === 'chat',
        'page-login': $route.name === 'login',
        'page-settings': $route.path === '/config',
      }"
    >
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, watch, provide } from "vue";
import { useRouter } from "vue-router";
import * as authApi from "./api/auth.js";
import AppIcon from "./components/AppIcon.vue";

const router = useRouter();
const username = ref(localStorage.getItem("username") || "");
const role = ref(localStorage.getItem("role") || "user");

// 登录/退出都会跳转路由，此时用户名/角色可能已变化 → 同步一次
watch(
  () => router.currentRoute.value.fullPath,
  () => {
    username.value = localStorage.getItem("username") || "";
    role.value = localStorage.getItem("role") || "user";
  },
);

provide("accountActions", (action) => {
  if (action === "password") showPwdModal.value = true;
  else if (action === "logout") logout();
  else if (action === "delete") deleteAccount();
});

// ── 修改密码 ──
const showPwdModal = ref(false);
const oldPwd = ref("");
const newPwd = ref("");
const pwdMsg = ref("");
const pwdErr = ref(false);
const pwdLoading = ref(false);

async function doChangePwd() {
  pwdMsg.value = "";
  if (newPwd.value.length < 4) {
    pwdErr.value = true;
    pwdMsg.value = "新密码至少 4 位";
    return;
  }
  pwdLoading.value = true;
  try {
    await authApi.changePassword(oldPwd.value, newPwd.value);
    pwdErr.value = false;
    pwdMsg.value = "✅ 修改成功，请重新登录";
    setTimeout(() => {
      showPwdModal.value = false;
      logout();
    }, 1200);
  } catch (err) {
    pwdErr.value = true;
    pwdMsg.value = err.suggestion || err.message || "修改失败";
  } finally {
    pwdLoading.value = false;
  }
}

// ── 注销账号 ──
async function deleteAccount() {
  if (
    !confirm(
      "确定注销当前账号？该账号的会话和上传文件将被永久删除，且不可恢复！",
    )
  )
    return;
  try {
    await authApi.deleteAccount();
    alert("账号已注销");
    logout();
  } catch (err) {
    alert("注销失败：" + (err.suggestion || err.message));
  }
}

function logout() {
  localStorage.removeItem("token");
  localStorage.removeItem("username");
  localStorage.removeItem("role");
  router.push("/login");
}
</script>

<style>
/* ── Design System Tokens ── */
:root {
  --bg-primary: #ffffff;
  --bg-secondary: #f7f8fa;
  --bg-tertiary: #f2f3f5;
  --text-primary: #1d2129;
  --text-secondary: #4e5969;
  --text-tertiary: #86909c;
  --brand-start: #1f1f1f;
  --brand-end: #444444;
  --brand-gradient: linear-gradient(135deg, #1f1f1f 0%, #444444 100%);
  --border-color: #e5e6eb;
  --border-light: #f0f1f3;
  --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.06);
  --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.08);
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 24px;
  --font-stack:
    -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", Arial,
    sans-serif;
  --font-mono: "SF Mono", "Fira Code", "Consolas", monospace;
  --transition: all 0.2s ease;
}

/* ── Reset & Base ── */
*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
html {
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
body {
  font-family: var(--font-stack);
  color: var(--text-primary);
  background: var(--bg-secondary);
  line-height: 1.6;
}

/* ── App Shell ── */
.app {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* ── Topbar ── */
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-color);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.02);
}
.topbar-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 17px;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}
.brand-icon {
  flex: none;
}

/* ── Navigation ── */
.nav {
  display: flex;
  align-items: center;
  gap: 4px;
}
.navlink {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  text-decoration: none;
  transition: var(--transition);
}
.navlink:hover {
  color: var(--text-primary);
  background: var(--bg-tertiary);
}
.navlink.active {
  color: var(--brand-start);
  background: rgba(0, 0, 0, 0.06);
  font-weight: 600;
}

/* ── 顶栏用户区 ── */
.topbar-user {
  display: flex;
  align-items: center;
  gap: 10px;
}
.topbar-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.topbar-role {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--radius-xl);
  font-weight: 600;
  flex: none;
}
.topbar-role.admin {
  background: var(--brand-gradient);
  color: #fff;
}
.topbar-role.user {
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}
.topbar-logout {
  background: none;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: var(--transition);
}
.topbar-logout:hover {
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.3);
}
.topbar-logout.del {
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.3);
}
.topbar-logout.del:hover {
  background: rgba(239, 68, 68, 0.06);
}

/* ── 修改密码弹窗 ── */
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 999;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
}
.modal {
  width: 340px;
  background: var(--bg-primary);
  border-radius: var(--radius-lg);
  padding: 24px;
  box-shadow: var(--shadow-lg);
}
.modal-title {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 16px;
}
.modal-field {
  margin-bottom: 12px;
}
.modal-field label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.modal-field input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 14px;
  color: var(--text-primary);
}
.modal-field input:focus {
  outline: none;
  border-color: var(--brand-start);
}
.modal-msg {
  font-size: 13px;
  margin-bottom: 10px;
  color: #16a34a;
}
.modal-msg.err {
  color: #ef4444;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.modal-btn {
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition);
}
.modal-btn.ghost {
  background: none;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}
.modal-btn.ghost:hover {
  border-color: var(--border-color);
  background: var(--bg-tertiary);
}
.modal-btn.primary {
  background: var(--brand-gradient);
  border: none;
  color: #fff;
}
.modal-btn.primary:hover:not(:disabled) {
  opacity: 0.92;
}
.modal-btn.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Page Container ── */
.page {
  flex: 1;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
  padding: 20px 24px;
}
/* 聊天页通栏：去掉左右留白，让历史会话贴左、对话区贴右 */
.page-fluid {
  max-width: none;
  padding: 20px 0;
}

/* 登录页只占顶栏下的剩余空间，避免子页 100vh 叠加顶栏高度。 */
.app-login {
  height: 100vh;
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
}
.app-login .topbar {
  flex-shrink: 0;
}
.page-login {
  display: flex;
  min-height: 0;
  max-width: none;
  padding: 0;
  overflow: hidden;
}
@media (max-width: 600px) {
  .app-login .topbar-inner {
    padding: 0 20px;
  }
  .app-login .nav {
    display: none;
  }
}

/* ── Scrollbar ── */
::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: #e5e6eb;
  border-radius: 3px;
}
::-webkit-scrollbar-thumb:hover {
  background: #c0c2c8;
}

/* ── Selection ── */
::selection {
  background: rgba(0, 0, 0, 0.12);
}
</style>

<style src="./styles/workspace.css"></style>
