import { defineStore } from 'pinia'
import * as cfgApi from '../api/config.js'

const SID_KEY = 'da_session_id'

// randomUUID 只在安全上下文（HTTPS 或 localhost）中可用。
// 公网 HTTP 首次部署时仍需能生成会话 ID，避免应用在挂载前中断并显示空白页。
export function createSessionId() {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()

  const bytes = new Uint8Array(16)
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const stored_sid = localStorage.getItem(SID_KEY) || createSessionId()

export const useConfigStore = defineStore('config', {
  state: () => ({
    llmList: [], dbList: [],
    currentLlmId: null, currentDbId: null,
    sessionId: stored_sid
  }),
  getters: {
    currentLlm: (s) => s.llmList.find((x) => x.id === s.currentLlmId) || null,
    currentDb:  (s) => s.dbList.find((x) => x.id === s.currentDbId) || null,
    ready: (s) => !!s.currentLlmId && !!s.currentDbId
  },
  actions: {
    async loadAll() {
      const [llm, db] = await Promise.all([cfgApi.listLlm(), cfgApi.listDb()])
      this.llmList = llm || []; this.dbList = db || []
      this.currentLlmId = (this.currentLlmId && this.llmList.some((x) => x.id === this.currentLlmId))
        ? this.currentLlmId : (this.llmList.find((x) => x.is_default)?.id ?? this.llmList[0]?.id ?? null)
      this.currentDbId = (this.currentDbId && this.dbList.some((x) => x.id === this.currentDbId))
        ? this.currentDbId : (this.dbList.find((x) => x.is_default)?.id ?? this.dbList[0]?.id ?? null)
    },
    newSession() {
      this.sessionId = createSessionId()
      localStorage.setItem(SID_KEY, this.sessionId)
    },
    setSession(id) {
      this.sessionId = id
      localStorage.setItem(SID_KEY, id)
    }
  }
})
