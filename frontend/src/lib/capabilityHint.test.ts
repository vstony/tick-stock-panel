import { expect, test } from 'vitest'

import { unavailableCapabilityHint } from './capabilityHint'

test('有候选源时提示可切换到谁 (未接入 ≠ 不支持)', () => {
  const hint = unavailableCapabilityHint({
    label: '全量分钟',
    candidates: [
      { name: 'tushare_api', display: 'Tushare (HTTP 自定义源)' },
      { name: 'myfm', display: 'MyFM' },
    ],
  })

  expect(hint.text).toBe('未接入 · 可切到 Tushare (HTTP 自定义源)')
  expect(hint.tone).toBe('warn')
  expect(hint.title).toContain('Tushare (HTTP 自定义源) / MyFM')
})

test('没有候选源时保持原来的未接入文案', () => {
  const hint = unavailableCapabilityHint({ label: '五档盘口', candidates: [] })

  expect(hint.text).toBe('未接入')
  expect(hint.tone).toBe('muted')
  expect(hint.title).toContain('暂无可用提供方')
})
