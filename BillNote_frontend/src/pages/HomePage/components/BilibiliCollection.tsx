import { useEffect, useRef, useState } from 'react'
import { v4 as uuid } from 'uuid'
import { ListVideo, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { generateCollection, getVideoCollection } from '@/services/collection'
import type { CollectionSubmission, GenerationOptions, VideoCollection } from '@/services/collection'
import { useTaskStore } from '@/store/taskStore'

interface Props {
  videoUrl: string
  getOptions: () => Promise<GenerationOptions | null>
  onBusyChange: (busy: boolean) => void
}

function errorMessage(error: unknown) {
  if (error && typeof error === 'object') {
    if ('detail' in error && typeof error.detail === 'string') return error.detail
    if ('msg' in error && typeof error.msg === 'string') return error.msg
  }
  return '请求失败，请稍后重试'
}

function durationLabel(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export default function BilibiliCollection({ videoUrl, getOptions, onBusyChange }: Props) {
  const [collection, setCollection] = useState<VideoCollection | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [submitted, setSubmitted] = useState<number | null>(null)
  const [batch, setBatch] = useState<CollectionSubmission | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const request = useRef<AbortController | null>(null)
  const submitLock = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      request.current?.abort()
    }
  }, [])

  const preview = async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    try {
      const result = await getVideoCollection(videoUrl, controller.signal)
      if (controller.signal.aborted) return
      setCollection(result)
      setSelected([result.currentPage])
      setSubmitted(null)
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error))
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  const prepare = async () => {
    if (submitLock.current) return
    submitLock.current = true
    try {
      const options = await getOptions()
      if (!options || !mounted.current) return
      // 确认时冻结本批参数；网络不确定时只允许以同一标识重试。
      setBatch({ ...options, request_id: uuid(), pages: [...selected] })
      setAttempted(false)
      setError('')
      setConfirmOpen(true)
      onBusyChange(true)
    } finally {
      submitLock.current = false
    }
  }

  const submit = async () => {
    if (!batch || submitLock.current) return
    submitLock.current = true
    setSubmitting(true)
    setAttempted(true)
    setError('')
    try {
      const result = await generateCollection(batch)
      // 不切换当前任务，避免历史刷新把正在填写的表单重置。
      await useTaskStore.getState().syncHistory()
      if (!mounted.current) return
      setSubmitted(result.total)
      setConfirmOpen(false)
      setBatch(null)
      setAttempted(false)
      onBusyChange(false)
    } catch (error) {
      if (mounted.current) {
        setError(errorMessage(error))
        // 明确被拒绝的请求可取消后修改配置；断网则保留同一批次重试。
        if (error && typeof error === 'object' && (
          ('detail' in error && Array.isArray(error.detail)) || ('code' in error && error.code === 300102)
        )) setAttempted(false)
      }
    } finally {
      submitLock.current = false
      if (mounted.current) setSubmitting(false)
    }
  }

  const closeConfirm = (open: boolean) => {
    if (submitting) return
    setConfirmOpen(open)
    if (!open && !attempted) {
      setBatch(null)
      onBusyChange(false)
    }
  }

  return (
    <section className="rounded-lg border bg-muted/20 p-3" aria-label="B站视频选集">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium"><ListVideo className="h-4 w-4" />视频选集</div>
        <Button type="button" variant="outline" size="sm" disabled={!videoUrl.trim() || loading || !!batch} onClick={preview}>
          {loading && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
          {loading ? '读取目录中…' : collection ? '重新读取目录' : '查看视频选集'}
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">支持同一 BV 的多 P 视频；查看目录不会下载视频或生成笔记。</p>
      {collection && (
        <div className="mt-3 space-y-3">
          <h3 className="break-words text-sm font-medium">{collection.title}</h3>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span>共 {collection.total} 集 · 当前 P{collection.currentPage} · 已选 {selected.length} 集</span>
            <div className="flex gap-1">
              <Button type="button" variant="ghost" size="sm" disabled={!!batch || submitted !== null} onClick={() => setSelected(collection.episodes.map(item => item.page))}>全选</Button>
              <Button type="button" variant="ghost" size="sm" disabled={!!batch || submitted !== null} onClick={() => setSelected([])}>清空</Button>
            </div>
          </div>
          <div className="max-h-64 overflow-y-auto rounded-md border bg-background">
            {collection.episodes.map(episode => (
              <label key={episode.page} className="flex cursor-pointer items-start gap-2 border-b px-3 py-2 text-sm last:border-b-0 hover:bg-muted/50">
                <Checkbox className="mt-0.5 shrink-0" aria-label={`选择 P${episode.page} ${episode.title}`} checked={selected.includes(episode.page)} disabled={!!batch || submitted !== null}
                  onCheckedChange={checked => setSelected(previous => checked ? [...previous, episode.page] : previous.filter(page => page !== episode.page))} />
                <span className="w-8 shrink-0 text-muted-foreground">P{episode.page}</span>
                <span className="min-w-0 flex-1 break-words">{episode.title}{episode.page === collection.currentPage && <span className="ml-2 text-xs text-primary">当前集</span>}</span>
                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{durationLabel(episode.duration)}</span>
              </label>
            ))}
          </div>
          {submitted !== null ? (
            <p role="status" className="text-sm text-primary">已提交 {submitted} 集任务，请在「生成历史」查看进度。重新读取目录后可开始新批次。</p>
          ) : batch && attempted ? (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">上次提交结果尚未确认。请勿新建重复批次；重试将核对并复用同一批任务。</p>
              <Button type="button" variant="outline" className="w-full" onClick={() => setConfirmOpen(true)}>核对 / 重试同一批次</Button>
            </div>
          ) : (
            <Button type="button" className="w-full" disabled={!selected.length || !!batch} onClick={prepare}>生成所选 {selected.length} 集笔记</Button>
          )}
        </div>
      )}
      {error && !confirmOpen && <p role="alert" className="mt-2 break-words text-sm text-destructive">{error}</p>}
      <Dialog open={confirmOpen} onOpenChange={closeConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认生成 {batch?.pages.length} 集笔记？</DialogTitle>
            <DialogDescription>每集会单独创建任务，按目录顺序逐集处理，并使用确认时选择的模型和笔记设置。可能产生转写和模型调用费用；关闭本页面不会取消已提交任务。服务重启不会自动恢复尚未执行的队列。</DialogDescription>
          </DialogHeader>
          <p className="max-h-24 overflow-y-auto break-words text-sm">所选分集：{batch?.pages.slice().sort((a, b) => a - b).map(page => `P${page}`).join('、')}</p>
          <p className="text-sm text-muted-foreground">模型：{batch?.model_name}</p>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}。可重试同一批次；如已接收，不会重复创建任务。也可关闭弹窗先核对生成历史。</p>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={submitting} onClick={() => closeConfirm(false)}>{attempted ? '关闭并核对历史' : '取消'}</Button>
            <Button type="button" disabled={submitting} onClick={submit}>{submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{submitting ? '提交中…' : attempted ? '重试同一批次' : '确认并加入队列'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
