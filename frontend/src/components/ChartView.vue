<template>
  <section class="chart-view" :class="{ expanded }">
    <div v-if="imageBase64" class="static-chart">
      <img :src="`data:image/png;base64,${imageBase64}`" alt="图表" class="chart-image" />
    </div>
    <div v-if="parsedChartConfig" class="chart-container" :class="{ 'with-image': imageBase64 }">
      <div ref="chartRef" class="echarts-chart" :style="{ height: `${chartHeight}px` }"></div>
    </div>
    <div v-if="!parsedChartConfig && !imageBase64" class="empty-state"><p>暂无图表数据</p></div>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  chartConfig: Object,
  imageBase64: String,
  expanded: { type: Boolean, default: false }
})

const chartRef = ref(null)
const chartHeight = computed(() => props.expanded ? 520 : 360)
let chartInstance = null
let resizeObserver = null

const parsedChartConfig = computed(() => {
  if (!props.chartConfig) return null
  if (typeof props.chartConfig === 'string') {
    try { return JSON.parse(props.chartConfig) } catch { return null }
  }
  return props.chartConfig
})

async function updateChart() {
  await nextTick()
  if (!chartRef.value || !parsedChartConfig.value) return
  if (!chartInstance) {
    chartInstance = echarts.init(chartRef.value)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => chartInstance?.resize())
      resizeObserver.observe(chartRef.value.parentElement || chartRef.value)
    }
  }
  chartInstance.setOption(parsedChartConfig.value, true)
  chartInstance.resize()
}

onMounted(updateChart)
watch(() => [parsedChartConfig.value, props.imageBase64, props.expanded], updateChart, { deep: true, flush: 'post' })
onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<style scoped>
.chart-view { width: 100%; min-width: 0; color: var(--text-primary); }
.static-chart { margin-bottom: 14px; text-align: center; }
.chart-image { display: block; max-width: 100%; max-height: 70vh; margin-inline: auto; object-fit: contain; }
.chart-container { width: 100%; min-width: 0; }
.echarts-chart { width: 100%; transition: height 0.24s ease; }
.empty-state { padding: 36px 12px; text-align: center; color: var(--text-secondary); }
.empty-state p { margin: 0; }
</style>
