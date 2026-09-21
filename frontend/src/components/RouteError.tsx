import { isRouteErrorResponse, useRouteError } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { Logo } from '@/components/Logo'
import { isDynamicImportError } from '@/lib/lazyWithRetry'

/**
 * 路由级兜底页: 页面模块取不到或页面渲染抛错时, 给出可操作的提示与「重新加载」按钮,
 * 不停留在 react-router 默认的英文开发提示页。
 */
export function RouteError() {
  const error = useRouteError()
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : String(error ?? '未知错误')
  const chunkFailed = isDynamicImportError(error)

  return (
    <div className="grid min-h-screen place-items-center bg-base px-6">
      <div className="w-full max-w-md rounded-card border border-border bg-surface p-6 text-center">
        <Logo size={28} className="mx-auto text-foreground" />
        <div className="mt-3 text-sm font-medium text-foreground">页面加载失败</div>
        <div className="mt-1.5 text-xs leading-relaxed text-muted">
          {chunkFailed
            ? '页面模块没能取到, 通常是开发服务器重启或版本更新导致的。点击重新加载即可恢复。'
            : '页面渲染时出错。重新加载后若仍然失败, 请把下方信息反馈给开发者。'}
        </div>
        <div className="mt-3 max-h-32 overflow-auto rounded border border-border bg-elevated px-2 py-1.5 text-left text-[11px] text-muted break-all">
          {message}
        </div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-4 inline-flex items-center gap-1.5 rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新加载
        </button>
      </div>
    </div>
  )
}
