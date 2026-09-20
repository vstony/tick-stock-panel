/** 数据源能力徽标里的「未接入」提示文案。
 *
 * 未接入只说明**当前路由的源供不了这个能力**, 不等于该能力没人支持: 声明了数据集的自定义源
 * (YAML/插件) 会出现在候选里, 用户点一下候选就能切过去。两种情况混在一起显示「未接入」会把
 * 「没切换」当成「不支持」(实测困惑: 已在 YAML 里声明 full_minute, 徽标仍是未接入)。
 */

export type CapabilityCandidate = {
  name: string
  display: string
}

export type CapabilityHint = {
  text: string
  title: string
  tone: 'warn' | 'muted'
}

export function unavailableCapabilityHint(cap: {
  label: string
  candidates: CapabilityCandidate[]
}): CapabilityHint {
  const displays = cap.candidates.map(c => c.display)
  if (displays.length === 0) {
    return {
      text: '未接入',
      title: `「${cap.label}」暂无可用提供方 — 点此前往数据源配置`,
      tone: 'muted',
    }
  }
  return {
    text: `未接入 · 可切到 ${displays[0]}`,
    title: `「${cap.label}」当前路由的源供不了该能力 — 点此前往数据源配置, 切到 ${displays.join(' / ')}`,
    tone: 'warn',
  }
}
