<template>
  <div class="db-config-form">
    <form :aria-busy="saving" @submit.prevent="onSubmit">
      <div class="field">
        <label>配置名称</label>
        <input v-model="form.name" placeholder="例如：生产数据库" autofocus required />
      </div>
      <div class="field">
        <label>主机</label>
        <input v-model="form.host" placeholder="localhost" required />
      </div>
      <div class="field-row">
        <div class="field">
          <label>端口</label>
          <input v-model.number="form.port" type="number" placeholder="3306" required />
        </div>
        <div class="field">
          <label>数据库类型</label>
          <select v-model="form.db_type" required>
            <option value="mysql">MySQL</option>
            <option value="postgresql">PostgreSQL</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>数据库名</label>
        <input v-model="form.database" placeholder="mydb" required />
      </div>
      <div class="field-row">
        <div class="field">
          <label>用户名</label>
          <input v-model="form.username" placeholder="root" required />
        </div>
        <div class="field">
          <label>密码</label>
          <input v-model="form.password" type="password" placeholder="••••••" />
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

const initialData = props.initialData || {}
const form = reactive({
  name: initialData.name || '',
  host: initialData.host || 'localhost',
  port: initialData.port || 3306,
  db_type: initialData.db_type || 'mysql',
  database: initialData.default_schema || initialData.database || '',
  username: initialData.username || 'root',
  password: initialData.password || ''
})

// 编辑时回填：initialData 变化（点列表项）→ 同步表单
watch(() => props.initialData, (v) => {
  const p = v || {}
  form.name = p.name || ''
  form.host = p.host || 'localhost'
  form.port = p.port || 3306
  form.db_type = p.db_type || 'mysql'
  form.database = p.default_schema || p.database || ''
  form.username = p.username || 'root'
  form.password = p.password || ''
}, { deep: true })

const onSubmit = () => {
  if (!props.saving) emit('submit', { ...form })
}
</script>

<style scoped>
.db-config-form {
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
