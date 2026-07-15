import { DependencyList, useEffect, useRef } from 'react'

export function usePolling(
  callback: () => void | Promise<void>,
  intervalMs: number,
  deps: DependencyList = [],
  enabled: boolean = true,
) {
  // 用 ref 持有最新 callback,避免内联箭头函数每次 render 变化导致 effect 重注册
  // (旧实现把 callback 放进 deps → 每次 render 都重跑 effect → 立即触发请求 → 请求风暴)
  const savedCallback = useRef(callback)
  savedCallback.current = callback
  useEffect(() => {
    if (!enabled) return
    const tick = () => {
      void savedCallback.current()
    }
    tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, ...deps])
}
