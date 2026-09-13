import { useEffect, useMemo, useState } from 'react'
import { Archive, ArrowDownUp, ChevronDown, FolderPlus, Pencil, RefreshCw, Trash } from 'lucide-react'
import toast from 'react-hot-toast'
import { useTaskStore, type Task } from '@/store/taskStore'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { assignNotes, createArchiveJob, createCategory, deleteCategory, getArchiveConfig,
  listArchiveJobs, listCategories, renameCategory, retryArchiveJob,
  type ArchiveJob, type Category } from '@/services/library'

const control = 'rounded-md border border-neutral-200 bg-white px-2 py-1.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-40'
const archiveLabels: Record<string, string> = { UNARCHIVED: '未入库', ARCHIVED: '已入库', OUTDATED: '待更新',
  QUEUED: '排队中', RUNNING: '整理中', FAILED: '入库失败', COMPLETED: '已入库' }

export default function LibraryHistory({ onSelect, selectedId }: {
  onSelect: (id: string) => void; selectedId: string | null
}) {
  const tasks = useTaskStore(state => state.tasks)
  const syncHistory = useTaskStore(state => state.syncHistory)
  const syncError = useTaskStore(state => state.syncError)
  const removeTask = useTaskStore(state => state.removeTask)
  const [categories, setCategories] = useState<Category[]>([])
  const [jobs, setJobs] = useState<ArchiveJob[]>([])
  const [ready, setReady] = useState(false)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState('created-desc')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [dialog, setDialog] = useState<{ id?: string; assign?: boolean } | null>(null)
  const [name, setName] = useState('')

  const refresh = async () => {
    const [nextCategories, nextJobs, config] = await Promise.all([listCategories(), listArchiveJobs(), getArchiveConfig()])
    setCategories(nextCategories); setJobs(nextJobs); setReady(config.ready)
  }
  useEffect(() => {
    let active = true
    const update = async () => {
      try {
        const [nextCategories, nextJobs, config] = await Promise.all([listCategories(), listArchiveJobs(), getArchiveConfig()])
        if (active) { setCategories(nextCategories); setJobs(nextJobs); setReady(config.ready) }
      } catch { /* 保留已加载的分类，历史区域显示同步失败提示。 */ }
    }
    void update()
    const timer = window.setInterval(update, 5000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  useEffect(() => { setSelected(ids => ids.filter(id => tasks.some(task => task.id === id))) }, [tasks])

  const visible = useMemo(() => tasks.filter(task => {
    if (filter === 'uncategorized' && task.categoryId) return false
    if (filter === 'categorized' && !task.categoryId) return false
    return (task.audioMeta.title || '').toLocaleLowerCase().includes(search.toLocaleLowerCase().trim())
  }).sort((a, b) => {
    const comparison = sort.startsWith('name')
      ? (a.audioMeta.title || '').localeCompare(b.audioMeta.title || '', 'zh-CN', { numeric: true })
      : new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime()
    return (sort.endsWith('asc') ? comparison : -comparison) || a.id.localeCompare(b.id)
  }), [tasks, filter, search, sort])
  const sortedCategories = [...categories].sort((a, b) => {
    const comparison = sort.startsWith('name') ? a.name.localeCompare(b.name, 'zh-CN', { numeric: true })
      : new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime()
    return sort.endsWith('asc') ? comparison : -comparison
  })
  const unclassified = tasks.filter(task => !task.categoryId).length
  const allSelected = visible.length > 0 && visible.every(task => selected.includes(task.id))
  const run = async (action: () => Promise<unknown>, message?: string) => {
    if (busy) return
    setBusy(true)
    try { await action(); await Promise.all([syncHistory(), refresh()]); if (message) toast.success(message) }
    catch { /* 请求层统一展示可读错误。 */ }
    finally { setBusy(false) }
  }
  const archive = (ids: string[], categoryId?: string) => run(async () => {
    const job = await createArchiveJob(ids, categoryId)
    toast.success(job.status === 'COMPLETED' ? '该版本已经入库，无需重复整理' : '入库任务已提交，可关闭网页等待 QQ 提醒')
  })
  const move = (value: string) => {
    if (!value) return
    if (value === 'new') { setName(''); setDialog({ assign: true }); return }
    void run(async () => { await assignNotes(selected, value === 'none' ? null : value); setSelected([]) }, '归类已保存')
  }
  const saveCategory = () => run(async () => {
    if (dialog?.id) await renameCategory(dialog.id, name)
    else {
      const category = await createCategory(name)
      if (dialog?.assign) { await assignNotes(selected, category.id); setSelected([]) }
    }
    setDialog(null)
  }, '分类已保存')

  const renderNote = (task: Task) => {
    const job = jobs.find(item => item.snapshot.notes.some(note => note.id === task.id))
    const archiveState = job && ['QUEUED', 'RUNNING', 'FAILED'].includes(job.status)
      ? job.status : task.archiveStatus || 'UNARCHIVED'
    return <div key={task.id} className={`rounded-lg border p-2.5 ${selectedId === task.id ? 'border-primary bg-primary-light' : 'border-neutral-200 bg-white'}`}>
      <div className="flex items-start gap-2">
        <input type="checkbox" aria-label={`选择 ${task.audioMeta.title || '未命名笔记'}`} checked={selected.includes(task.id)}
          onChange={event => setSelected(ids => event.target.checked ? [...ids, task.id] : ids.filter(id => id !== task.id))}
          className="mt-1 h-4 w-4 shrink-0 accent-primary" />
        <button onClick={() => onSelect(task.id)} className="min-w-0 flex-1 text-left text-sm font-medium leading-5 text-neutral-900">
          <span className="line-clamp-2">{task.audioMeta.title || '未命名笔记'}</span>
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-neutral-500">
        <span>{new Date(task.createdAt).toLocaleDateString('zh-CN')}</span>
        <span className="rounded bg-neutral-100 px-1.5 py-0.5">{task.categoryId ? '已归类' : '未归类'}</span>
        <span className={`rounded px-1.5 py-0.5 ${archiveState === 'ARCHIVED' ? 'bg-emerald-50 text-emerald-700' : archiveState === 'FAILED' ? 'bg-red-50 text-red-700' : 'bg-neutral-100'}`}>
          {archiveLabels[archiveState]}
        </span>
        {task.status !== 'SUCCESS' && <span>{task.status === 'FAILED' ? '生成失败' : '生成中'}</span>}
      </div>
      <div className="mt-2 flex items-center justify-end gap-2">
        <button className={`${control} flex items-center gap-1`} disabled={!ready || busy || task.status !== 'SUCCESS'}
          onClick={() => void archive([task.id])}><Archive size={12} />手动入库</button>
        <button className="rounded p-1 text-neutral-400 hover:bg-red-50 hover:text-red-600" aria-label={`删除 ${task.audioMeta.title}`}
          disabled={busy} onClick={() => { if (window.confirm('将这条笔记从生成历史移除？已入库的文件会保留。')) void run(() => removeTask(task.id)) }}><Trash size={13} /></button>
      </div>
    </div>
  }

  return <div className="flex min-w-0 flex-col gap-3 pb-6">
    <div className="flex gap-2">
      <input aria-label="搜索笔记标题" placeholder="搜索笔记标题…" value={search} onChange={event => setSearch(event.target.value)} className={`${control} min-w-0 flex-1`} />
      <button className={control} aria-label="刷新历史" disabled={busy} onClick={() => void run(async () => {})}><RefreshCw size={14} /></button>
      <button className={control} title="创建分类" aria-label="创建分类" onClick={() => { setName(''); setDialog({}) }}><FolderPlus size={14} /></button>
    </div>
    {syncError && <div role="alert" className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">{syncError}</div>}
    <div className="flex flex-wrap gap-1">
      {([['all', `全部 ${tasks.length}`], ['uncategorized', `未归类 ${unclassified}`], ['categorized', `已归类 ${tasks.length - unclassified}`]]).map(([value, label]) =>
        <button key={value} onClick={() => setFilter(value)} aria-pressed={filter === value}
          className={`rounded-full px-2.5 py-1 text-xs ${filter === value ? 'bg-neutral-900 text-white' : 'bg-neutral-100 text-neutral-600'}`}>{label}</button>)}
    </div>
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-neutral-500">
      <label className="flex items-center gap-1.5"><input type="checkbox" checked={allSelected} aria-label="全选当前筛选结果"
        onChange={event => setSelected(event.target.checked ? [...new Set([...selected, ...visible.map(task => task.id)])] : selected.filter(id => !visible.some(task => task.id === id)))} />全选</label>
      <div className="flex items-center gap-1"><ArrowDownUp size={12} /><select aria-label="排序方式" className={control} value={sort} onChange={event => setSort(event.target.value)}>
        <option value="created-desc">时间：从新到旧</option><option value="created-asc">时间：从旧到新</option>
        <option value="name-asc">名称：升序</option><option value="name-desc">名称：降序</option>
      </select></div>
    </div>
    {selected.length > 0 && <div className="flex flex-wrap items-center gap-2 rounded-lg bg-neutral-100 p-2">
      <span className="text-xs">已选 {selected.length} 条</span>
      <select aria-label="批量归类" value="" disabled={busy} className={`${control} max-w-32`} onChange={event => move(event.target.value)}>
        <option value="">归入分类…</option><option value="new">新建分类并归入</option><option value="none">取消归类</option>
        {categories.map(category => <option key={category.id} value={category.id}>{category.name}</option>)}
      </select>
      <button className={control} disabled={busy || !ready} onClick={() => void archive(selected)}>手动入库</button>
      <button className="text-xs text-neutral-500" onClick={() => setSelected([])}>取消选择</button>
    </div>}
    {!ready && <p className="text-[11px] text-neutral-500">入库连接待配置；可以正常归类和查看历史。</p>}
    {filter !== 'uncategorized' && sortedCategories.map(category => {
      const notes = visible.filter(task => task.categoryId === category.id)
      if (search && !notes.length) return null
      return <details key={category.id} open className="rounded-lg border border-neutral-200 bg-neutral-50">
        <summary className="flex cursor-pointer list-none items-center gap-1.5 p-2.5 text-sm font-medium">
          <ChevronDown size={14} /><span className="min-w-0 flex-1 truncate" title={category.name}>{category.name}</span>
          <span className="text-xs font-normal text-neutral-500">{category.count}</span>
        </summary>
        <div className="flex flex-wrap items-center gap-2 px-2.5 pb-2 text-[10px] text-neutral-500">
          <span>{archiveLabels[category.archiveStatus]}</span>
          <button className={control} disabled={!ready || busy || !category.count} onClick={() => void archive([], category.id)}>整类入库</button>
          <button aria-label={`重命名分类 ${category.name}`} onClick={() => { setName(category.name); setDialog({ id: category.id }) }}><Pencil size={12} /></button>
          <button aria-label={`解散分类 ${category.name}`} disabled={busy} onClick={() => {
            if (window.confirm(`解散“${category.name}”？笔记将变为未归类，原始内容和已入库文件会保留。`)) void run(() => deleteCategory(category.id), '分类已解散')
          }}><Trash size={12} /></button>
        </div>
        <div className="flex flex-col gap-2 px-2 pb-2">{notes.map(renderNote)}{!notes.length && <p className="p-2 text-xs text-neutral-400">暂无笔记</p>}</div>
      </details>
    })}
    {filter !== 'categorized' && <div className="flex flex-col gap-2">
      <p className="text-xs font-medium text-neutral-500">未归类</p>
      {visible.filter(task => !task.categoryId).map(renderNote)}
    </div>}
    {!visible.length && <p className="py-4 text-center text-xs text-neutral-400">没有匹配的笔记</p>}
    {jobs.length > 0 && <details className="border-t border-neutral-200 pt-3">
      <summary className="cursor-pointer text-xs font-medium">最近入库任务</summary>
      <div className="mt-2 flex flex-col gap-2">{jobs.slice(0, 10).map(job => <div key={job.id} className="rounded bg-neutral-50 p-2 text-xs">
        <p>{job.snapshot.category?.name || `${job.snapshot.notes.length} 条笔记`} · {archiveLabels[job.status] || job.status}</p>
        {job.error && <p className="mt-1 break-words text-red-600">{job.error}</p>}
        {job.notification === 'FAILED' && <p className="text-amber-700">整理完成，QQ 通知发送失败</p>}
        {job.status === 'FAILED' && <button className={`${control} mt-1`} disabled={busy} onClick={() => void run(() => retryArchiveJob(job.id))}>重试</button>}
      </div>)}</div>
    </details>}
    <Dialog open={dialog !== null} onOpenChange={open => { if (!open) setDialog(null) }}>
      <DialogContent aria-describedby="library-category-description"><DialogHeader><DialogTitle>{dialog?.id ? '重命名分类' : '创建分类'}</DialogTitle></DialogHeader>
        <form className="flex flex-col gap-3" onSubmit={event => { event.preventDefault(); void saveCategory() }}>
          <label className="text-sm" htmlFor="category-name">分类名称</label>
          <input id="category-name" autoFocus required maxLength={100} value={name} onChange={event => setName(event.target.value)} className={control} />
          <p id="library-category-description" className="text-xs text-neutral-500">保存分类不会触发入库；之后可手动整理单条、多条或整个分类。</p>
          <button type="submit" disabled={busy || !name.trim()} className={`${control} self-end bg-neutral-900 text-white`}>保存</button>
        </form>
      </DialogContent>
    </Dialog>
  </div>
}
