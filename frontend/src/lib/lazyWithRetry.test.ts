import { expect, test, vi } from 'vitest'

import { importWithRetry, isDynamicImportError, lazyWithRetry } from './lazyWithRetry'

// 用户实际看到的报错原文 (dev 下 Dashboard 模块取不到)
const CHUNK_ERROR = 'Failed to fetch dynamically imported module: http://localhost:3011/src/pages/Dashboard.tsx'
const OTHER_CHUNK_ERROR = 'Failed to fetch dynamically imported module: http://localhost:3011/src/pages/Watchlist.tsx'

class FakeStorage {
  private data = new Map<string, string>()
  getItem(key: string) {
    return this.data.get(key) ?? null
  }
  setItem(key: string, value: string) {
    this.data.set(key, value)
  }
}

const fail = (message = CHUNK_ERROR) => () => Promise.reject(new Error(message))

test('识别各内核/Vite 的模块取不到文案, 不误判普通错误', () => {
  expect(isDynamicImportError(new Error(CHUNK_ERROR))).toBe(true)
  expect(isDynamicImportError(new Error('error loading dynamically imported module'))).toBe(true)
  expect(isDynamicImportError(new Error('Importing a module script failed.'))).toBe(true)
  expect(isDynamicImportError(new Error('504 Outdated Optimize Dep'))).toBe(true)
  expect(isDynamicImportError('Failed to fetch dynamically imported module')).toBe(true)

  expect(isDynamicImportError(new Error('Cannot read properties of undefined'))).toBe(false)
  expect(isDynamicImportError(new Error('请求失败: 500'))).toBe(false)
  expect(isDynamicImportError(null)).toBe(false)
})

test('换代失败自动刷新一次; 同一模块在窗口内再失败则原样抛出', async () => {
  const reload = vi.fn()
  const storage = new FakeStorage()

  await expect(importWithRetry(fail(), { reload, storage, now: () => 1000 })).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(1)

  // 刷新后同一模块仍失败(窗口内) → 不再刷新, 交给 errorElement
  await expect(importWithRetry(fail(), { reload, storage, now: () => 10_000 })).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(1)
})

test('刷新标记按模块区分: 另一个模块失败仍会自动刷新', async () => {
  const reload = vi.fn()
  const storage = new FakeStorage()

  await expect(importWithRetry(fail(), { reload, storage, now: () => 1000 })).rejects.toThrow(CHUNK_ERROR)
  await expect(importWithRetry(fail(OTHER_CHUNK_ERROR), { reload, storage, now: () => 1_100 })).rejects.toThrow()
  expect(reload).toHaveBeenCalledTimes(2)
})

test('超过刷新窗口后同一模块允许再次刷新', async () => {
  const reload = vi.fn()
  const storage = new FakeStorage()

  await expect(importWithRetry(fail(), { reload, storage, now: () => 1000 })).rejects.toThrow(CHUNK_ERROR)
  await expect(importWithRetry(fail(), { reload, storage, now: () => 11_000 })).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(2)
})

test('加载成功不写标记, 后续同一模块失败仍会自动刷新', async () => {
  const reload = vi.fn()
  const storage = new FakeStorage()

  await expect(importWithRetry(async () => 'ok', { storage, now: () => 1000 })).resolves.toBe('ok')
  await expect(importWithRetry(fail(), { reload, storage, now: () => 1500 })).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(1)
})

test('非模块加载类错误不刷新, 原样抛出', async () => {
  const reload = vi.fn()
  const storage = new FakeStorage()
  const error = new Error('Cannot read properties of undefined')

  await expect(importWithRetry(() => Promise.reject(error), { reload, storage, now: () => 1000 })).rejects.toBe(error)
  expect(reload).not.toHaveBeenCalled()
})

test('sessionStorage 不可用时同一模块也不会反复刷新', async () => {
  // 时间戳取极大值: 进程内兜底是模块级状态, 测试之间会残留
  const reload = vi.fn()
  const deps = { reload, storage: null, now: () => 1_000_000_000 }

  await expect(importWithRetry(fail(), deps)).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(1)

  await expect(importWithRetry(fail(), { ...deps, now: () => 1_000_000_100 })).rejects.toThrow(CHUNK_ERROR)
  expect(reload).toHaveBeenCalledTimes(1)

  // 另一个模块仍有自己的刷新机会
  await expect(importWithRetry(fail(OTHER_CHUNK_ERROR), { ...deps, now: () => 1_000_000_200 })).rejects.toThrow()
  expect(reload).toHaveBeenCalledTimes(2)
})

test('lazyWithRetry 返回真正的 React.lazy 组件', () => {
  const Lazy = lazyWithRetry(async () => ({ default: () => null }))
  expect((Lazy as { $$typeof?: symbol }).$$typeof).toBe(Symbol.for('react.lazy'))
})
