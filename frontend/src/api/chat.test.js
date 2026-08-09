import { describe, it, expect, vi, beforeEach } from 'vitest'
import { streamChat } from './chat.js'

/** 把若干 SSE data 帧包装成 fetch 可用的 ReadableStream 响应 */
function sseResponse(frames) {
  const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body))
      controller.close()
    }
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('streamChat SSE 解析', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('请求携带 Authorization token（fetch 不走 axios 拦截器）', async () => {
    localStorage.setItem('token', 'jwt-test')
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([{ type: 'done', session_id: 's1', reply: 'ok' }]))
    vi.stubGlobal('fetch', fetchMock)
    await streamChat({ message: 'hi' })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers.Authorization).toBe('Bearer jwt-test')
  })

  it('逐帧回调 onDelta 并在 done 时回调 onDone', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      { type: 'delta', text: '你好' },
      { type: 'delta', text: '，世界' },
      { type: 'done', session_id: 's1', reply: 'ok' }
    ])))
    const onDelta = vi.fn()
    const onDone = vi.fn()
    await streamChat({ message: 'hi' }, { onDelta, onDone })
    expect(onDelta.mock.calls.flat()).toEqual(['你好', '，世界'])
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ session_id: 's1' }))
  })

  it('error 帧回调 onError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      { type: 'error', error: { code: 'LLM_QUOTA', message: '余额不足' } }
    ])))
    const onError = vi.fn()
    await streamChat({ message: 'hi' }, { onError })
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'LLM_QUOTA' }))
  })

  it('trace 帧回调 onTrace（工具调用链实时增量）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      { type: 'trace', entries: [{ seq: 1, tool: 'sql_expert', status: 'ok' }] },
      { type: 'trace', entries: [{ seq: 2, tool: 'query_mysql', status: 'ok' }] },
      { type: 'done', session_id: 's1', reply: 'ok' }
    ])))
    const onTrace = vi.fn()
    await streamChat({ message: 'hi' }, { onTrace })
    expect(onTrace).toHaveBeenCalledTimes(2)
    expect(onTrace.mock.calls[0][0]).toEqual([{ seq: 1, tool: 'sql_expert', status: 'ok' }])
    expect(onTrace.mock.calls[1][0][0].tool).toBe('query_mysql')
  })

  it('非 2xx 响应 → onError + 抛错', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('boom', { status: 500 })))
    const onError = vi.fn()
    await expect(streamChat({ message: 'hi' }, { onError })).rejects.toMatchObject({ code: 'HTTP_500' })
    expect(onError).toHaveBeenCalledOnce()
  })

  it('网络失败 → onError(NETWORK)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed')))
    const onError = vi.fn()
    await expect(streamChat({}, { onError })).rejects.toMatchObject({ code: 'NETWORK' })
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'NETWORK' }))
  })

  it('跨 chunk 的帧边界也能正确解析', async () => {
    const frame = 'data: ' + JSON.stringify({ type: 'delta', text: '跨块' }) + '\n\n'
    const half = Math.floor(frame.length / 2)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame.slice(0, half)))
        controller.enqueue(new TextEncoder().encode(frame.slice(half)))
        controller.close()
      }
    }), { status: 200 })))
    const onDelta = vi.fn()
    await streamChat({}, { onDelta })
    expect(onDelta).toHaveBeenCalledWith('跨块')
  })

  it('连接关闭时没有最后空行也能处理 done 帧', async () => {
    const body = `data: ${JSON.stringify({ type: 'done', session_id: 's1', reply: '完成' })}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(body))
        controller.close()
      }
    }), { status: 200 })))
    const onDone = vi.fn()
    await streamChat({}, { onDone })
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ reply: '完成' }))
  })
})
