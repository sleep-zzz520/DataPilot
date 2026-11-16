import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentTrace from './AgentTrace.vue'

const entries = [
  { seq: 1, agent: 'supervisor', tool: 'data_executor', depth: 0, input: '{"request":"查订单"}', output: '完成', status: 'ok', duration_ms: 120 },
  { seq: 2, agent: 'data_executor', tool: 'query_mysql', depth: 1, input: '{"sql":"SELECT 1"}', output: '1 行', status: 'error', duration_ms: 30 }
]

describe('AgentTrace', () => {
  it('entries 为空时不渲染', () => {
    const w = mount(AgentTrace, { props: { entries: [] } })
    expect(w.find('.trace').exists()).toBe(false)
  })

  it('渲染每步工具、Agent 与状态', () => {
    const w = mount(AgentTrace, { props: { entries } })
    expect(w.text()).toContain('Agent 调用轨迹')
    expect(w.text()).toContain('2 步')
    expect(w.text()).toContain('data_executor')
    expect(w.text()).toContain('query_mysql')
    expect(w.text()).toContain('主管')
    expect(w.text()).toContain('数据执行')
    expect(w.text()).toContain('成功')
    expect(w.text()).toContain('失败')
    expect(w.text()).toContain('120ms')
  })

  it('点击展开入参与结果摘要', async () => {
    const w = mount(AgentTrace, { props: { entries } })
    expect(w.find('.trace-detail').exists()).toBe(false)  // 默认折叠
    const steps = w.findAll('.trace-step')
    await steps[1].trigger('click')  // 展开第二行（query_mysql）
    expect(w.find('.trace-detail').exists()).toBe(true)
    expect(w.text()).toContain('SELECT 1')
    expect(w.text()).toContain('入参')
    expect(w.text()).toContain('结果')
  })

  it('再次点击收起详情', async () => {
    const w = mount(AgentTrace, { props: { entries } })
    const step = w.findAll('.trace-step')[1]
    await step.trigger('click')
    expect(w.find('.trace-detail').exists()).toBe(true)
    await step.trigger('click')
    expect(w.find('.trace-detail').exists()).toBe(false)
  })

  it('compact 模式按运行状态默认展开/收起，且手动选择优先', async () => {
    const w = mount(AgentTrace, { props: { compact: true, entries: [entries[0]] } })
    expect(w.find('.trace-list').exists()).toBe(false)
    await w.setProps({ entries: [{ ...entries[0], status: 'running' }] })
    expect(w.find('.trace-list').exists()).toBe(true)
    await w.find('.trace-toggle').trigger('click')
    await w.setProps({ entries: [{ ...entries[0], status: 'ok' }] })
    expect(w.find('.trace-list').exists()).toBe(false)
  })

  it('新增轨迹步骤时保留已展开步骤，并支持键盘操作', async () => {
    const w = mount(AgentTrace, { props: { entries: [entries[0]] } })
    await w.find('.trace-main').trigger('keydown.enter')
    expect(w.find('.trace-detail').exists()).toBe(true)
    await w.setProps({ entries: [...entries, { seq: 3, agent: 'agent', tool: 'new_tool', status: 'ok' }] })
    expect(w.findAll('.trace-detail')).toHaveLength(1)
  })
})
