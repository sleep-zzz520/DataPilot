import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

const chart = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }
vi.mock('echarts', () => ({ init: vi.fn(() => chart) }))
import * as echarts from 'echarts'
import ChartView from './ChartView.vue'

describe('ChartView', () => {
  afterEach(() => vi.clearAllMocks())

  it('有静态图片时不显示空状态', () => {
    const wrapper = mount(ChartView, { props: { imageBase64: 'abc' } })
    expect(wrapper.find('.chart-image').exists()).toBe(true)
    expect(wrapper.find('.empty-state').exists()).toBe(false)
  })

  it('挂载和配置变化后初始化或更新图表，expanded 控制高度', async () => {
    const wrapper = mount(ChartView, { props: { chartConfig: { series: [] } } })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(echarts.init).toHaveBeenCalledOnce()
    expect(chart.setOption).toHaveBeenCalledWith({ series: [] }, true)
    expect(wrapper.find('.echarts-chart').attributes('style')).toContain('360px')
    await wrapper.setProps({ expanded: true, chartConfig: { series: [{ type: 'bar' }] } })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(wrapper.find('.echarts-chart').attributes('style')).toContain('520px')
    expect(chart.resize).toHaveBeenCalled()
    wrapper.unmount()
    expect(chart.dispose).toHaveBeenCalledOnce()
  })
})
