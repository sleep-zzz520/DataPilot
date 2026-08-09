import http from './http.js'
export const chat = (payload) => http.post('/api/chat', payload)

// 流式对话（SSE）：回调 onDelta(文本增量) / onTrace(轨迹增量) / onDone(最终结果) / onError(错误对象)
export async function streamChat(payload, { onDelta, onTrace, onPlan, onDone, onError } = {}) {
  let resp
  try {
    resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // fetch 不走 axios 拦截器，需手动带 token（否则后端 401）
        Authorization: `Bearer ${localStorage.getItem('token') || ''}`
      },
      body: JSON.stringify(payload)
    })
  } catch (_) {
    const e = { code:'NETWORK', severity:'block', message:'无法连接到后端服务。', suggestion:'请确认后端已启动（uvicorn ... 8000），且 vite 代理目标正确。' }
    onError && onError(e)
    throw e
  }
  if (!resp.ok || !resp.body) {
    const e = { code:`HTTP_${resp.status}`, severity:'block', message:`请求失败（${resp.status}）。`, suggestion:'后端流式接口异常。' }
    onError && onError(e)
    throw e
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  const dispatchFrame = (frame) => {
    for (const line of frame.split('\n')) {
      if (!line.startsWith('data: ')) continue
      let ev
      try { ev = JSON.parse(line.slice(6)) } catch (_) { continue }
      if (ev.type === 'delta' && onDelta) onDelta(ev.text || '')
      else if (ev.type === 'trace' && onTrace) onTrace(ev.entries || [])
      else if (ev.type === 'plan' && onPlan) onPlan(ev.plan || null)
      else if (ev.type === 'done' && onDone) onDone(ev)
      else if (ev.type === 'error' && onError) onError(ev.error)
    }
  }
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let sep
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      dispatchFrame(frame)
    }
  }
  // 兼容代理/开发服务器在连接关闭时未补最后一个空行的情况。
  if (buf.trim()) dispatchFrame(buf)
}
export const upload = (file) => { const fd = new FormData(); fd.append('file', file); return http.post('/api/upload', fd) }
export const schema = (dbConfigId) => http.get('/api/schema', { params: { db_config_id: dbConfigId } })
export const clearChat = (sessionId) => http.delete(`/api/chat/${sessionId}`)
// 对话记录持久化
export const listSessions = (limit = 50) => http.get('/api/sessions', { params: { limit } })
export const getSession = (sessionId) => http.get(`/api/sessions/${sessionId}`)
export const deleteSession = (sessionId) => http.delete(`/api/sessions/${sessionId}`)
export const renameSession = (sessionId, title) => http.put(`/api/sessions/${sessionId}`, { title })
