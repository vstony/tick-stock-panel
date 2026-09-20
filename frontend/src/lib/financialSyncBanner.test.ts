import { expect, test } from 'vitest'

import { FULL_SYNC_HINT, financialSyncBannerText, formatElapsed } from './financialSyncBanner'

test('全量同步中给出当前表与已用时 (不是干等一个 0/5)', () => {
  const text = financialSyncBannerText({
    synced: 0,
    total: 5,
    tableLabel: '财务指标',
    elapsedMs: 192_000,
  })

  expect(text).toBe('已同步 0/5 张表… · 正在同步 财务指标 · 已用时 3分12秒')
})

test('表名未知时省略该段, 计数与用时仍在', () => {
  const text = financialSyncBannerText({ synced: 2, total: 5, tableLabel: null, elapsedMs: 45_000 })

  expect(text).toBe('已同步 2/5 张表… · 已用时 45秒')
})

test('已用时格式: 秒/分秒/小时分', () => {
  expect(formatElapsed(0)).toBe('0秒')
  expect(formatElapsed(59_000)).toBe('59秒')
  expect(formatElapsed(3_600_000)).toBe('1小时0分')
  expect(formatElapsed(7_860_000)).toBe('2小时11分')
})

test('提示文案说明慢的原因与替代做法', () => {
  expect(FULL_SYNC_HINT).toContain('按标的逐个请求')
  expect(FULL_SYNC_HINT).toContain('单表同步')
})
