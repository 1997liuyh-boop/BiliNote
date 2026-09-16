import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { get as readStorage, set as writeStorage, del } from 'idb-keyval'
import { generateNote } from '@/services/note'
import { deleteLibraryNote, getLibraryNote, importLibraryNotes, listLibraryNotes } from '@/services/library'
import toast from 'react-hot-toast'

export type TaskStatus = 'PENDING' | 'RUNNING' | 'PARSING' | 'DOWNLOADING' | 'TRANSCRIBING' |
  'SUMMARIZING' | 'FORMATTING' | 'SAVING' | 'SUCCESS' | 'FAILED'
export interface AudioMeta {
  cover_url: string
  duration: number
  file_path: string
  platform: string
  raw_info: any
  title: string
  video_id: string
}
export interface Segment { start: number; end: number; text: string }
export interface Transcript { full_text: string; language: string; raw: any; segments: Segment[] }
export interface Markdown { ver_id: string; content: string; style: string; model_name: string; created_at: string }
export interface Task {
  id: string
  markdown: string | Markdown[]
  transcript: Transcript
  status: TaskStatus
  audioMeta: AudioMeta
  createdAt: string
  updatedAt?: string
  platform: string
  formData: Record<string, any>
  categoryId?: string | null
  archiveJob?: { id: string; status: string; stage: string } | null
  archiveStatus?: string
  archivePath?: string
  revision?: string
  contentLoaded?: boolean
}
interface TaskStore {
  tasks: Task[]
  currentTaskId: string | null
  libraryMigrated: boolean
  syncError: string
  syncing: boolean
  addPendingTask: (id: string, platform: string, formData: Record<string, any>) => void
  updateTaskContent: (id: string, data: Partial<Task>) => void
  removeTask: (id: string) => Promise<void>
  clearTasks: () => void
  setCurrentTask: (id: string | null) => void
  getCurrentTask: () => Task | null
  retryTask: (id: string, payload?: any) => Promise<void>
  syncHistory: () => Promise<void>
  loadTask: (id: string) => Promise<void>
}

const emptyTranscript: Transcript = { full_text: '', language: '', raw: null, segments: [] }

export const useTaskStore = create<TaskStore>()(persist((set, get) => ({
  tasks: [], currentTaskId: null, libraryMigrated: false, syncError: '', syncing: false,
  addPendingTask: (id, platform, formData) => set(state => ({
    tasks: [{ id, platform, formData, status: 'PENDING', markdown: [], transcript: emptyTranscript,
      createdAt: new Date().toISOString(), categoryId: null, archiveStatus: 'UNARCHIVED',
      audioMeta: { cover_url: '', duration: 0, file_path: '', platform, raw_info: null, title: '', video_id: '' },
    }, ...state.tasks.filter(task => task.id !== id)], currentTaskId: id,
  })),
  updateTaskContent: (id, data) => set(state => ({
    tasks: state.tasks.map(task => task.id === id ? { ...task, ...data } : task),
  })),
  getCurrentTask: () => get().tasks.find(task => task.id === get().currentTaskId) || null,
  loadTask: async id => {
    try {
      const task = await getLibraryNote(id)
      set(state => ({ tasks: state.tasks.map(item => item.id === id ? {
        ...task, contentLoaded: true,
        formData: JSON.stringify(item.formData) === JSON.stringify(task.formData) ? item.formData : task.formData,
      } : item) }))
    } catch {
      set({ syncError: '笔记内容加载失败，请重试' })
    }
  },
  setCurrentTask: id => {
    set({ currentTaskId: id })
    if (id) void get().loadTask(id)
  },
  removeTask: async id => {
    await deleteLibraryNote(id)
    set(state => ({ tasks: state.tasks.filter(task => task.id !== id),
      currentTaskId: state.currentTaskId === id ? null : state.currentTaskId }))
    toast.success('笔记已从历史移除')
  },
  // 清理当前浏览器缓存，不删除服务器记录。
  clearTasks: () => set({ tasks: [], currentTaskId: null }),
  retryTask: async (id, payload) => {
    const task = get().tasks.find(item => item.id === id)
    if (!task) return
    const formData = payload || task.formData
    await generateNote({ ...formData, task_id: id })
    get().updateTaskContent(id, { status: 'PENDING', formData })
  },
  syncHistory: async () => {
    if (get().syncing) return
    set({ syncing: true })
    try {
      // 新接口可用且迁移成功前，完整保留浏览器中的旧版本。
      await listLibraryNotes()
      if (!get().libraryMigrated) {
        const legacyTasks = get().tasks
        for (let offset = 0; offset < legacyTasks.length; offset += 20) {
          await importLibraryNotes(legacyTasks.slice(offset, offset + 20))
        }
        set({ libraryMigrated: true })
      }
      const incoming: Task[] = []
      let total = 0
      do {
        const page = await listLibraryNotes(incoming.length)
        incoming.push(...page.items)
        total = page.total
        if (!page.items.length) break
      } while (incoming.length < total)
      set(state => ({ tasks: incoming.map(task => {
        const cached = state.tasks.find(item => item.id === task.id)
        if (cached && JSON.stringify(cached.formData) === JSON.stringify(task.formData)) task.formData = cached.formData
        return cached?.contentLoaded && cached.revision === task.revision
          ? { ...task, markdown: cached.markdown, transcript: cached.transcript, contentLoaded: true }
          : task
      }), syncError: '' }))
      const selected = get().getCurrentTask()
      if (selected && !selected.contentLoaded) await get().loadTask(selected.id)
      if (get().currentTaskId && !selected) set({ currentTaskId: null })
    } catch {
      set({ syncError: '服务器历史同步失败，当前显示本机缓存，可点击重试' })
    } finally {
      set({ syncing: false })
    }
  },
}), {
  name: 'task-storage',
  partialize: state => ({ tasks: state.tasks, currentTaskId: state.currentTaskId, libraryMigrated: state.libraryMigrated }),
  storage: createJSONStorage(() => ({
    getItem: async name => (await readStorage(name)) ?? null,
    setItem: async (name, value) => { await writeStorage(name, value) },
    removeItem: async name => { await del(name) },
  })),
}))
