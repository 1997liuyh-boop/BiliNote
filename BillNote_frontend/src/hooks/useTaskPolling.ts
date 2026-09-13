import { useEffect } from 'react'
import { useTaskStore } from '@/store/taskStore'

export const useTaskPolling = (interval = 5000, enabled = true) => {
  useEffect(() => {
    if (!enabled) return
    const sync = () => { if (useTaskStore.persist.hasHydrated()) void useTaskStore.getState().syncHistory() }
    const unsubscribe = useTaskStore.persist.onFinishHydration(sync)
    sync()
    const timer = window.setInterval(sync, interval)
    window.addEventListener('focus', sync)
    return () => { clearInterval(timer); unsubscribe(); window.removeEventListener('focus', sync) }
  }, [interval, enabled])
}
