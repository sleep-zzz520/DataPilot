import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import DataTable from './DataTable.vue'

describe('DataTable', () => {
  it('兼容后端 table 结构的数组行', () => {
    const w = mount(DataTable, {
      props: {
        table: {
          columns: ['region', 'total'],
          rows: [['East', 200], ['West', 80]]
        }
      }
    })
    expect(w.findAll('th').map(th => th.text())).toEqual(['region', 'total'])
    const rows = w.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].findAll('td').map(td => td.text())).toEqual(['East', '200'])
    expect(rows[1].findAll('td').map(td => td.text())).toEqual(['West', '80'])
  })

  it('保留 columns/data 对象行用法', () => {
    const w = mount(DataTable, {
      props: {
        columns: ['name', 'score'],
        data: [{ name: 'Alice', score: 90 }]
      }
    })
    expect(w.find('tbody td').text()).toBe('Alice')
    expect(w.findAll('tbody td')[1].text()).toBe('90')
  })

  it('previewLimit 默认显示全部，设置后裁剪预览且提示总行数', () => {
    const data = Array.from({ length: 4 }, (_, id) => ({ id, amount: id * 1.25 }))
    const all = mount(DataTable, { props: { columns: ['id', 'amount'], data } })
    expect(all.findAll('tbody tr')).toHaveLength(4)
    const preview = mount(DataTable, { props: { columns: ['id', 'amount'], data, previewLimit: 2 } })
    expect(preview.findAll('tbody tr')).toHaveLength(2)
    expect(preview.text()).toContain('显示 2 行，共 4 行')
    expect(preview.findAll('td.numeric')).toHaveLength(4)
  })
})
