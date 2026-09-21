import { lazy, type ComponentType, type LazyExoticComponent } from 'react'

/**
 * 动态 import 失败兜底。
 *
 * 这类失败通常不是代码错, 而是模块图换代:
 * - dev: 文件保存/改写的瞬间 dev server 返回 500, 或 dev server 重启、依赖预构建重跑(旧 URL 失效);
 * - prod: 页面停留在旧版本, 服务器上的旧 chunk 已被新版本覆盖。
 *
 * 两种情况下刷新一次页面即可拿到新模块图, 所以这里自动刷新一次。
 * 刷新标记按「失败的模块 URL」区分, 并且不做清除:
 * - 同一模块在窗口内再次失败 → 说明刷新解决不了问题, 原样抛出, 交给路由 errorElement 展示;
 * - 其他模块仍各自拥有一次刷新机会(同一次换代常同时影响多个页面 chunk);
 * - 版本更新后 chunk URL 会变, 新 URL 自然拿到新的刷新机会。
 * 只在窗口内限流, 是为了避免「刷新 → 仍失败 → 再刷新」的死循环。
 */
const RELOAD_KEY = 'tsp:dynamic-import-reload-at'
// 同一模块在这个时间窗口内只触发一次自动刷新
const RELOAD_WINDOW_MS = 10_000

// 各内核/工具链在模块取不到时的实际报错文案
const IMPORT_FAILURE_HINTS = [
  'failed to fetch dynamically imported module', // Chromium / Firefox (type=module)
  'error loading dynamically imported module', // Firefox
  'importing a module script failed', // Safari
  'outdated optimize dep', // Vite 依赖预构建过期(504)
  'unable to preload css', // Vite 产物缺失同包 CSS
]

export function isDynamicImportError(error: unknown): boolean {
  const message = (error instanceof Error ? error.message : String(error ?? '')).toLowerCase()
  return IMPORT_FAILURE_HINTS.some(hint => message.includes(hint))
}

// 从报错文案里取出失败模块的 URL; 取不到(如 Safari 只有一句文案)则退回全局标记
function reloadMarkKey(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? '')
  const url =
    message.match(/https?:\/\/\S+/)?.[0] ??
    message.match(/\/[^\s'"]+\.(?:m?jsx?|tsx?|css)(?:\?\S*)?/)?.[0]
  return url ? `${RELOAD_KEY}:${url}` : RELOAD_KEY
}

type ImportRetryDeps = {
  reload?: () => void
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  now?: () => number
}

// sessionStorage 不可用(隐私模式等)时的进程内兜底, 保证「同一模块只刷新一次」的语义不因此失效
const inMemoryReloads = new Map<string, number>()

function sessionStorageOrNull(): ImportRetryDeps['storage'] {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

/**
 * 执行动态 import; 失败且判定为模块图换代时自动刷新页面一次,
 * 同一模块在刷新窗口内重复失败则原样抛出。
 */
export async function importWithRetry<T>(
  factory: () => Promise<T>,
  deps: ImportRetryDeps = {},
): Promise<T> {
  const reload = deps.reload ?? (() => window.location.reload())
  const storage = deps.storage === undefined ? sessionStorageOrNull() : deps.storage
  const now = deps.now ?? Date.now

  try {
    return await factory()
  } catch (error) {
    if (!isDynamicImportError(error)) throw error

    const key = reloadMarkKey(error)
    const readReloadedAt = () => {
      if (storage) {
        try {
          return Number(storage.getItem(key) ?? 0) || 0
        } catch {
          // 存储读取异常 → 退回进程内兜底
        }
      }
      return inMemoryReloads.get(key) ?? 0
    }

    const reloadedAt = readReloadedAt()
    // 刚为这个模块刷新过(标记仍在窗口内) → 刷新没解决问题, 保持抛出
    if (reloadedAt > 0 && now() - reloadedAt < RELOAD_WINDOW_MS) throw error

    const at = now()
    inMemoryReloads.set(key, at)
    try {
      storage?.setItem(key, String(at))
    } catch {
      // 存储写入失败不影响: 进程内兜底已保证同一模块只刷新一次
    }
    reload()
    throw error
  }
}

/**
 * 与 React.lazy 同签名, 只是给动态 import 加了一层「换代自动刷新」。
 * 命名导出页面用 `.then(m => ({ default: m.X }))` 映射后再传进来。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 与 React.lazy 官方签名保持一致
export function lazyWithRetry<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return lazy(() => importWithRetry(factory))
}
