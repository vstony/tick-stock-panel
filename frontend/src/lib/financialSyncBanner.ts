/** 财务页「全部同步」进行中的状态文案。
 *
 * 进度按**整表完成**计数, 而一张表要按标的逐个请求(YAML 自定义源如 Tushare 的
 * batch=1, 5568 只标的 × ~150ms ≈ 14 分钟) —— 只显示「已同步 0/5 张表…」会让
 * 用户以为卡死(实测反馈)。这里把「当前同步哪张表 + 已用时」一并给出, 并给出原因提示。
 */

export type SyncBannerInput = {
  synced: number
  total: number
  /** 当前正在同步的表名(中文标签); 全部完成/未知时为 null */
  tableLabel?: string | null
  elapsedMs: number
}

export const FULL_SYNC_HINT =
  '进度按整表计算: 每张表要按标的逐个请求(上游不支持一次取全市场), 首次全量约 10-20 分钟/表; 只想看核心指标时可先用单表同步'

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}小时${minutes}分`
  if (minutes > 0) return `${minutes}分${seconds}秒`
  return `${seconds}秒`
}

export function financialSyncBannerText(input: SyncBannerInput): string {
  const parts = [`已同步 ${input.synced}/${input.total} 张表…`]
  if (input.tableLabel) parts.push(`正在同步 ${input.tableLabel}`)
  parts.push(`已用时 ${formatElapsed(input.elapsedMs)}`)
  return parts.join(' · ')
}
