import { useMemo, useState } from 'react'
import { ChevronRight, FileText, Folder, FolderTree, RefreshCw, Search } from 'lucide-react'
import { getVaultTree, syncVault, type VaultSyncReport, type VaultTree } from '@/services/library'

const control = 'rounded-md border border-neutral-200 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-40'

interface TreeNode {
  name: string
  path: string
  children: TreeNode[]
  directory: boolean
}

function buildTree(paths: string[]) {
  const root: TreeNode = { name: '', path: '', children: [], directory: true }
  const nodes = new Map<string, TreeNode>([['', root]])
  for (const path of paths) {
    const parts = path.split('/')
    let parent = root
    parts.forEach((name, index) => {
      const key = parts.slice(0, index + 1).join('/')
      let node = nodes.get(key)
      if (!node) {
        node = { name, path: key, children: [], directory: index < parts.length - 1 }
        nodes.set(key, node)
        parent.children.push(node)
      }
      parent = node
    })
  }
  for (const node of nodes.values()) {
    node.children.sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name, 'zh-CN', { numeric: true }))
  }
  return root.children
}

function VaultNodes({ nodes, expand, depth = 0 }: { nodes: TreeNode[]; expand: boolean; depth?: number }) {
  return <ul className="min-w-0 space-y-0.5">
    {nodes.map(node => <li key={node.path} className="min-w-0">
      {node.directory ? <details open={expand || depth === 0} className="[&[open]>summary>svg:first-child]:rotate-90">
        <summary className="flex cursor-pointer list-none items-center gap-1.5 rounded px-1 py-1.5 text-neutral-700 hover:bg-neutral-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          <ChevronRight size={12} className="shrink-0 transition-transform" />
          <Folder size={14} className="shrink-0 text-amber-600" />
          <span className="min-w-0 truncate" title={node.path}>{node.name}</span>
        </summary>
        <div className="ml-2.5 border-l border-neutral-200 pl-2"><VaultNodes nodes={node.children} expand={expand} depth={depth + 1} /></div>
      </details> : <div className="flex items-start gap-1.5 rounded px-1 py-1.5 pl-5 text-neutral-600" title={node.path}>
        <FileText size={13} className="mt-0.5 shrink-0 text-neutral-400" /><span className="min-w-0 break-all">{node.name}</span>
      </div>}
    </li>)}
  </ul>
}

function errorMessage(error: unknown) {
  if (error && typeof error === 'object') {
    if ('detail' in error && typeof error.detail === 'string') return error.detail
    if ('msg' in error && typeof error.msg === 'string') return error.msg
  }
  return '无法读取 Vault，请检查服务器连接后重试。'
}

export default function VaultExplorer({ onSynced }: { onSynced: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [tree, setTree] = useState<VaultTree | null>(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState<'tree' | 'sync' | null>(null)
  const [error, setError] = useState('')
  const [report, setReport] = useState<VaultSyncReport | null>(null)
  const paths = useMemo(() => tree?.paths.filter(path => path.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) ?? [], [tree, query])
  const nodes = useMemo(() => buildTree(paths), [paths])

  const load = async () => {
    if (busy) return
    setBusy('tree'); setError('')
    try { setTree(await getVaultTree()) }
    catch (error) { setError(errorMessage(error)) }
    finally { setBusy(null) }
  }
  const synchronize = async () => {
    if (busy || !window.confirm('从 Vault 补齐未归类笔记及入库记录？仅匹配带 BiliNote ID 的文件；已有分类冲突将跳过。不会修改、移动或删除 Vault 文件。')) return
    setBusy('sync'); setError(''); setReport(null)
    try {
      const result = await syncVault()
      setReport(result)
      await onSynced()
      setTree(await getVaultTree())
    } catch (error) { setError(errorMessage(error)) }
    finally { setBusy(null) }
  }

  return <section className="overflow-hidden rounded-lg border border-neutral-200 bg-neutral-50/70" aria-label="服务器 Obsidian Vault">
    <button type="button" aria-expanded={open} aria-controls="vault-explorer-content" className="flex w-full items-center gap-2 p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary"
      onClick={() => { setOpen(!open); if (!open && !tree) void load() }}>
      <FolderTree size={16} className="shrink-0 text-neutral-600" />
      <span className="min-w-0 flex-1"><span className="block text-xs font-semibold text-neutral-800">Obsidian Vault</span><span className="block pt-0.5 text-[10px] text-neutral-500">服务器 Markdown 目录 · 只读</span></span>
      <ChevronRight size={14} className={`shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
    </button>
    {open && <div id="vault-explorer-content" className="space-y-3 border-t border-neutral-200 p-3" aria-busy={busy !== null}>
      <p className="text-[11px] leading-5 text-neutral-500">入库不等于归类。若已在 Vault 整理，可同步回 BiliNote；仅补齐未归类笔记，不覆盖已有分类。</p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={`${control} flex items-center gap-1`} disabled={busy !== null} onClick={() => void load()}><RefreshCw size={12} className={busy === 'tree' ? 'animate-spin' : ''} />刷新目录</button>
        <button type="button" className={`${control} flex-1`} disabled={busy !== null || !tree} onClick={() => void synchronize()}>{busy === 'sync' ? '正在核对文件…' : '同步归类与入库状态'}</button>
      </div>
      {error && <p role="alert" className="break-words rounded bg-red-50 p-2 text-xs leading-5 text-red-700">{error}{tree && ' 当前目录为上次读取结果。'}</p>}
      {busy && <p role="status" className="text-xs text-neutral-500">{busy === 'tree' ? '正在读取服务器目录…' : '正在核对笔记 ID 和分类，请勿重复提交。'}</p>}
      {report && <div role="status" className="space-y-1 rounded-md border border-emerald-100 bg-emerald-50 p-2 text-[11px] leading-5 text-emerald-900">
        <p className="font-medium">同步完成 · 匹配 {report.matched} 篇笔记</p>
        <p>补齐归类 {report.categorized} · 更新入库记录 {report.archived} · 无需更改 {report.unchanged}</p>
        <p>跳过非本库笔记 {report.skipped} · 冲突 {report.conflicts.length}</p>
        {report.unverified > 0 && <p>{report.unverified} 篇无法确认当前版本，已保留文件路径并标记待更新。</p>}
        {report.conflicts.length > 0 && <details className="text-amber-900"><summary className="cursor-pointer">查看需要手动确认的项目</summary><ul className="mt-1 max-h-40 space-y-2 overflow-y-auto">{report.conflicts.map((item, index) => <li key={`${item.path}-${index}`}><p className="break-all font-medium">{item.path}</p><p>{item.reason}</p></li>)}</ul></details>}
      </div>}
      {tree && <>
        <div className="flex items-center gap-1.5 rounded-md border border-neutral-200 bg-white px-2"><Search size={12} className="shrink-0 text-neutral-400" /><input aria-label="搜索 Vault 文件或目录" placeholder="搜索文件或目录…" value={query} onChange={event => setQuery(event.target.value)} className="min-w-0 flex-1 bg-transparent py-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary" /></div>
        <div className="flex flex-wrap justify-between gap-1 text-[10px] text-neutral-500"><span>{tree.folderCount} 个目录 · {tree.fileCount} 篇 Markdown</span><span>读取于 {new Date(tree.scannedAt).toLocaleTimeString('zh-CN')}</span></div>
        <div className="max-h-80 overflow-y-auto rounded-md border border-neutral-200 bg-white p-1.5 text-xs">
          {paths.length ? <VaultNodes key={query.trim() ? 'search' : 'browse'} nodes={nodes} expand={Boolean(query.trim())} /> : <p className="p-4 text-center text-neutral-400">{tree.fileCount ? '没有匹配的 Markdown 文件' : 'Vault 中暂无 Markdown 文件'}</p>}
        </div>
        <p className="text-[10px] leading-4 text-neutral-400">仅展示包含 .md 文件的目录；隐藏配置、附件和空目录不显示。</p>
      </>}
    </div>}
  </section>
}
