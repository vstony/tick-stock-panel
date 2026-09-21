import { createBrowserRouter, Navigate, useSearchParams } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { useSettings } from './lib/useSharedQueries'
import { Logo } from './components/Logo'
import { RouteError } from './components/RouteError'
import { lazyWithRetry } from './lib/lazyWithRetry'
import { ExtensionBoundary } from './extensions/ExtensionBoundary'
import {
  finalizeFrontendExtensions,
  getFrontendExtensionLoadErrors,
  getFrontendExtensionRoutes,
} from './extensions/registry'

// 代码分割: 页面全部懒加载, 避免首屏打包所有页面 (ECharts / lightweight-charts /
// framer-motion 等重库) → 大幅减小首屏 bundle。命名导出用 .then 映射为 default。
// Layout / Onboarding / Auth 为应用外壳与入口, 保持同步加载。
// 用 lazyWithRetry 而非 React.lazy: 模块图换代(dev server 重启/版本更新)导致 chunk 取不到时
// 自动刷新一次, 而不是把用户停在错误页。
const Watchlist = lazyWithRetry(() => import('./pages/Watchlist').then(m => ({ default: m.Watchlist })))
const Screener = lazyWithRetry(() => import('./pages/Screener').then(m => ({ default: m.Screener })))
const Backtest = lazyWithRetry(() => import('./pages/Backtest').then(m => ({ default: m.Backtest })))
const Factors = lazyWithRetry(() => import('./pages/Factors').then(m => ({ default: m.Factors })))
const Financials = lazyWithRetry(() => import('./pages/Financials').then(m => ({ default: m.Financials })))
const Data = lazyWithRetry(() => import('./pages/Data').then(m => ({ default: m.Data })))
const Monitor = lazyWithRetry(() => import('./pages/Monitor').then(m => ({ default: m.Monitor })))
const Lots = lazyWithRetry(() => import('./pages/Lots').then(m => ({ default: m.Lots })))
const Dashboard = lazyWithRetry(() => import('./pages/Dashboard').then(m => ({ default: m.Dashboard })))
const AnalysisDetail = lazyWithRetry(() => import('./pages/AnalysisDetail').then(m => ({ default: m.AnalysisDetail })))
const ConceptAnalysis = lazyWithRetry(() => import('./pages/ConceptAnalysis').then(m => ({ default: m.ConceptAnalysis })))
const IndustryAnalysis = lazyWithRetry(() => import('./pages/IndustryAnalysis').then(m => ({ default: m.IndustryAnalysis })))
const StockAnalysis = lazyWithRetry(() => import('./pages/StockAnalysis').then(m => ({ default: m.StockAnalysis })))
const Signals = lazyWithRetry(() => import('./pages/Signals').then(m => ({ default: m.Signals })))
const Review = lazyWithRetry(() => import('./pages/Review').then(m => ({ default: m.Review })))
const LimitUpLadder = lazyWithRetry(() => import('./pages/LimitUpLadder').then(m => ({ default: m.LimitUpLadder })))
const Indices = lazyWithRetry(() => import('./pages/Indices').then(m => ({ default: m.Indices })))
const Branding = lazyWithRetry(() => import('./pages/Branding').then(m => ({ default: m.Branding })))
const Settings = lazyWithRetry(() => import('./pages/Settings').then(m => ({ default: m.Settings })))
const Regime = lazyWithRetry(() => import('./pages/Regime').then(m => ({ default: m.Regime })))
const AbnormalMoves = lazyWithRetry(() => import('./pages/AbnormalMoves').then(m => ({ default: m.AbnormalMoves })))
const Dev = lazyWithRetry(() => import('./pages/Dev').then(m => ({ default: m.Dev })))

const CORE_ROUTE_PATHS = new Set([
  '/',
  '/onboarding',
  '/login',
  '/overview',
  '/analysis',
  '/analysis/:menuId',
  '/concept-analysis',
  '/industry-analysis',
  '/stock-analysis',
  '/review',
  '/watchlist',
  '/screener',
  '/backtest',
  '/factors',
  '/mining',
  '/financials',
  '/data',
  '/monitor',
  '/limit-ladder',
  '/indices',
  '/regime',
  '/abnormal',
  '/branding',
  '/settings',
  '/dev',
  '/settings/keys',
  '/settings/ai',
  '/settings/queries',
])

finalizeFrontendExtensions(CORE_ROUTE_PATHS)
const frontendExtensionRoutes = getFrontendExtensionRoutes()
const frontendExtensionErrors = getFrontendExtensionLoadErrors()
if (frontendExtensionErrors.length > 0) {
  console.error('部分前端扩展加载失败', frontendExtensionErrors)
}

// 旧链接兼容: 挖掘已并入因子页 (/factors?tab=mining), 保留 run/candidate 等参数重定向
function MiningRedirect() {
  const [searchParams] = useSearchParams()
  const search = searchParams.toString()
  return <Navigate to={`/factors?tab=mining${search ? `&${search}` : ''}`} replace />
}

// 首次使用守卫 —— 未完成向导则重定向到 /onboarding
// 只挂在根路由上;/onboarding 本身不被守卫,避免循环重定向。
// settings 由 Layout 预取,守卫判定不产生额外请求。
function OnboardingGuard({ children }: { children: React.ReactNode }) {
  const settings = useSettings()

  // 仅首次加载(本地无缓存)时显示占位。
  // 后台重取 (isFetching) 时本地已有上一份缓存可用, 直接放行, 避免切页时整屏 logo 闪烁。
  // 防误重定向已由 Onboarding/AI 等处 invalidate 前的 setQueryData 同步缓存兜底。
  if (settings.isLoading) {
    return (
      <div className="min-h-screen bg-base grid place-items-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <Logo size={28} className="text-foreground" />
          <div className="text-xs">加载中…</div>
        </div>
      </div>
    )
  }

  // 查询出错或字段缺失时不拦截 —— 宁可放行,也不把用户卡在空白页
  if (settings.data && settings.data.onboarding_completed === false) {
    return <Navigate to="/onboarding" replace />
  }

  return <>{children}</>
}

export const router = createBrowserRouter([
  { path: '/onboarding', element: <Onboarding />, errorElement: <RouteError /> },
  { path: '/login', element: <Auth />, errorElement: <RouteError /> },
  {
    path: '/',
    element: (
      <OnboardingGuard>
        <Layout />
      </OnboardingGuard>
    ),
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'overview', element: <Navigate to="/" replace /> },
      { path: 'analysis', element: <Navigate to="/settings?tab=ext-pages" replace /> },
      { path: 'analysis/:menuId', element: <AnalysisDetail /> },
      { path: 'concept-analysis', element: <ConceptAnalysis /> },
      { path: 'industry-analysis', element: <IndustryAnalysis /> },
      { path: 'stock-analysis', element: <StockAnalysis /> },
      { path: 'review', element: <Review /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'backtest', element: <Backtest /> },
      { path: 'factors', element: <Factors /> },
      { path: 'mining', element: <MiningRedirect /> },
      { path: 'financials', element: <Financials /> },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
      { path: 'lots', element: <Lots /> },
      { path: 'signals', element: <Signals /> },
      { path: 'limit-ladder', element: <LimitUpLadder /> },
      { path: 'indices', element: <Indices /> },
    { path: 'regime', element: <Regime /> },
      { path: 'abnormal', element: <AbnormalMoves /> },
      { path: 'branding', element: <Branding /> },
      { path: 'settings', element: <Settings /> },
      // 隐藏路由：开发者工具（不暴露在菜单，仅供调试）
      { path: 'dev', element: <Dev /> },
      // 旧路由兼容重定向
      { path: 'settings/keys', element: <Navigate to="/settings?tab=data-sources" replace /> },
      { path: 'settings/ai', element: <Navigate to="/settings?tab=ai" replace /> },
      { path: 'settings/queries', element: <Navigate to="/settings?tab=queries" replace /> },
      ...frontendExtensionRoutes.map(route => {
        const ExtensionPage = route.component
        return {
          path: route.path.slice(1),
          element: (
            <ExtensionBoundary extensionId={route.extensionId}>
              <ExtensionPage />
            </ExtensionBoundary>
          ),
        }
      }),
    ],
  },
])
