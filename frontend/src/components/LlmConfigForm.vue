<template>
  <div class="llm-config-form">
    <form :aria-busy="saving" @submit.prevent="onSubmit">
      <div class="field">
        <label>配置名称</label>
        <input v-model="form.name" placeholder="例如：GPT-4" autofocus required />
      </div>
      <div class="field">
        <label>API 提供商</label>
        <select v-model="form.provider" required>
          <option value="openai">OpenAI</option>
          <option value="anthropic">Anthropic</option>
          <option value="qwen">通义千问</option>
        </select>
      </div>
      <div class="field">
        <label>API 接口地址</label>
        <input v-model="form.base_url" placeholder="https://api.openai.com/v1" />
        <p class="field-hint">按提供商自动填充,可手动修改</p>
      </div>
      <div class="field">
        <label>API Key</label>
        <input v-model="form.api_key" type="password" placeholder="sk-..." required />
      </div>
      <div class="field">
        <label>模型名称</label>
        <input v-model="form.model_name" placeholder="gpt-4" required />
      </div>
      <div class="field-row">
        <div class="field">
          <label>温度</label>
          <input v-model.number="form.temperature" type="number" step="0.1" min="0" max="2" />
        </div>
        <div class="field">
          <label>最大 Token</label>
          <input v-model.number="form.max_tokens" type="number" placeholder="4096" />
        </div>
      </div>
      <div class="actions">
        <button type="button" class="cancel" :disabled="saving" @click="$emit('cancel')">取消</button>
        <button type="submit" class="submit" :disabled="saving">
          {{ saving ? '保存并测试中...' : '保存并测试' }}
        </button>
      </div>
    </form>
  </div>
</template>

<script setup>
import { reactive, watch } from 'vue'

const props = defineProps({
  initialData: { type: Object, default: () => ({}) },
  saving: { type: Boolean, default: false }
})

const emit = defineEmits(['submit', 'cancel'])

// 各提供商默认接口地址（与后端 factories.PROVIDER_DEFAULT_BASE_URL 保持一致）
const PROVIDER_URLS = {
  openai: 'https://api.openai.com/v1',
  anthropic: 'https://api.anthropic.com',
  qwen: 'https://dashscope.aliyuncs.com/compatible-mode/v1'
}

const initialData = props.initialData || {}
const initialProvider = initialData.provider || 'openai'
const form = reactive({
  name: initialData.name || '',
  provider: initialProvider,
  api_key: initialData.api_key || '',
  base_url: initialData.base_url || PROVIDER_URLS[initialProvider] || '',
  model_name: initialData.model_name || (initialProvider === 'qwen' ? 'qwen-plus' : 'gpt-4'),
  temperature: initialData.temperature ?? 0.7,
  max_tokens: initialData.max_tokens || 4096
})

// 切换提供商时联动：地址未手动改过则填充默认，模型名给合理默认
watch(() => form.provider, (np, op) => {
  const prevDefault = PROVIDER_URLS[op] || ''
  if (!form.base_url || form.base_url === prevDefault) {
    form.base_url = PROVIDER_URLS[np] || ''
  }
  if (np === 'qwen' && (form.model_name === 'gpt-4' || form.model_name === 'gpt-4o')) form.model_name = 'qwen-plus'
  if (np === 'openai' && form.model_name === 'qwen-plus') form.model_name = 'gpt-4'
  if (np === 'anthropic' && form.model_name === 'qwen-plus') form.model_name = 'claude-3-5-sonnet-latest'
})

// 编辑时回填：initialData 变化（点列表项）→ 同步表单
watch(() => props.initialData, (v) => {
  const p = v || {}
  const prov = p.provider || 'openai'
  form.name = p.name || ''
  form.provider = prov
  form.api_key = p.api_key || ''
  form.base_url = p.base_url || PROVIDER_URLS[prov] || ''
  form.model_name = p.model_name || (prov === 'qwen' ? 'qwen-plus' : 'gpt-4')
  form.temperature = p.temperature ?? 0.7
  form.max_tokens = p.max_tokens || 4096
}, { deep: true })

const onSubmit = () => {
  if (!props.saving) emit('submit', { ...form })
}
</script>

<style scoped>
.llm-config-form {
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 20px;
  box-shadow: var(--shadow-sm);
}

.field {
  margin-bottom: 16px;
}

.field label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
}
.field-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-tertiary);
}

.field input,
.field select {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 14px;
  color: var(--text-primary);
  background: var(--bg-primary);
  transition: var(--transition);
}

.field input:focus,
.field select:focus {
  outline: none;
  border-color: #1F1F1F;
  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.08);
}

.field-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 20px;
}

.cancel {
  padding: 8px 16px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  font-size: 14px;
  cursor: pointer;
  transition: var(--transition);
}

.cancel:hover {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}

.submit {
  padding: 8px 20px;
  background: var(--brand-gradient);
  border: none;
  border-radius: var(--radius-md);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

.submit:hover:not(:disabled) {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.28);
  transform: translateY(-1px);
}

.submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
