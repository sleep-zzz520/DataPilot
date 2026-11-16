<template>
  <div class="login-page">
    <div class="login-panel">
      <h2 class="login-title">{{ isRegister ? '注册账号' : '登录' }}</h2>
      <p v-if="isRegister" class="login-tip">
        新注册账号为<b>普通用户</b>；系统<b>首个注册</b>的账号自动成为<b>管理员</b>（可管理全局配置与用户）。
      </p>
      <p v-if="!isRegister && showTestCredentials" class="login-tip test-credentials">
        测试管理员：<b>admin</b> / <b>admin1234</b>
      </p>
      <form @submit.prevent="submit">
        <div class="field">
          <label for="login-username">用户名</label>
          <input id="login-username" v-model="username" autocomplete="username" placeholder="至少 2 位" :disabled="loading" required />
        </div>
        <div class="field">
          <label for="login-password">密码</label>
          <input id="login-password" v-model="password" type="password" :autocomplete="isRegister ? 'new-password' : 'current-password'" placeholder="至少 4 位" :disabled="loading" required />
        </div>
        <p v-if="error" class="login-error" role="alert">{{ error }}</p>
        <button type="submit" class="login-btn" :disabled="loading">
          {{ loading && !creatingTestAccount ? '请稍候...' : (isRegister ? '注册并登录' : '登录') }}
        </button>
      </form>
      <button type="button" class="login-btn test-account-btn" :disabled="loading" @click="addTestAccount">
        {{ creatingTestAccount ? '正在创建...' : '快速添加测试账号' }}
      </button>
      <p class="login-switch">
        {{ isRegister ? '已有账号？' : '还没有账号？' }}
        <button type="button" :disabled="loading" @click="toggleMode">{{ isRegister ? '去登录' : '去注册' }}</button>
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import * as authApi from '../api/auth.js'

const router = useRouter()
const isRegister = ref(false)
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)
const creatingTestAccount = ref(false)
const showTestCredentials = import.meta.env.DEV

function toggleMode() {
  isRegister.value = !isRegister.value
  error.value = ''
}

function completeLogin(res) {
  localStorage.setItem('token', res.token)
  localStorage.setItem('username', res.username)
  localStorage.setItem('role', res.role || 'user')
  router.push('/chat')
}

async function addTestAccount() {
  if (loading.value) return
  loading.value = true
  creatingTestAccount.value = true
  error.value = ''
  try {
    // 每次创建独立账号，权限与审计沿用普通注册流程。
    const random = Array.from(crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, '0')).join('')
    username.value = `test_${random.slice(0, 16)}`
    password.value = random.slice(16)
    const res = await authApi.register(username.value, password.value)
    completeLogin(res)
  } catch (err) {
    error.value = err.suggestion || err.message || '测试账号创建失败，请重试'
  } finally {
    loading.value = false
    creatingTestAccount.value = false
  }
}

async function submit() {
  if (loading.value) return
  if (username.value.trim().length < 2 || password.value.length < 4) {
    error.value = '用户名至少 2 位，密码至少 4 位'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = isRegister.value
      ? await authApi.register(username.value.trim(), password.value)
      : await authApi.login(username.value.trim(), password.value)
    completeLogin(res)
  } catch (err) {
    error.value = err.suggestion || err.message || '操作失败，请重试'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  flex: 1;
  min-width: 0; min-height: 0;
  display: flex;
  padding: 24px;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--bg-secondary);
}
.login-panel {
  width: 360px;
  max-width: 100%; flex-shrink: 0;
  margin: auto;
  animation: login-enter 0.2s ease-out;
}
.login-title { font-size: 24px; font-weight: 700; margin-bottom: 20px; }
.login-tip {
  font-size: 12px; color: var(--text-tertiary);
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
  padding: 8px 10px; margin-bottom: 14px; line-height: 1.6;
}
.login-tip b { color: var(--text-secondary); }
.field { margin-bottom: 14px; }
.field label { display: block; font-size: 13px; font-weight: 500; color: var(--text-secondary); margin-bottom: 6px; }
.field input {
  width: 100%; padding: 9px 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border-color); border-radius: var(--radius-sm);
  font-size: 14px; color: var(--text-primary);
}
.field input:focus { outline: none; border-color: var(--brand-start); }
.login-error { color: #EF4444; font-size: 13px; margin-bottom: 10px; }
.login-btn {
  width: 100%; padding: 10px;
  background: var(--brand-gradient); color: #fff; border: none;
  border-radius: var(--radius-sm); font-size: 14px; font-weight: 600;
  cursor: pointer; transition: var(--transition);
}
.login-btn:hover:not(:disabled) { opacity: 0.92; }
.login-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.test-account-btn { margin-top: 10px; background: transparent; color: var(--text-primary); border: 1px solid var(--border-color); }
.test-account-btn:hover:not(:disabled) { background: var(--bg-tertiary); border-color: var(--text-tertiary); }
.login-btn:focus-visible, .login-switch button:focus-visible { outline: 2px solid var(--brand-start); outline-offset: 3px; }
.login-switch { margin-top: 14px; font-size: 13px; color: var(--text-tertiary); text-align: center; }
.login-switch button { border: none; background: none; font: inherit; color: var(--brand-start); font-weight: 500; cursor: pointer; }
.login-switch button:disabled { opacity: 0.5; cursor: not-allowed; }
@keyframes login-enter { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
@media (max-height: 600px) {
  .login-page { padding: 12px 20px; }
  .login-title { margin-bottom: 12px; }
  .field { margin-bottom: 10px; }
  .login-switch { margin-top: 10px; }
}
@media (prefers-reduced-motion: reduce) { .login-panel { animation: none; } }
</style>
