import request from '@/utils/request'
import type { Task } from '@/store/taskStore'

export interface Category {
  id: string
  name: string
  count: number
  createdAt: string
  archiveStatus: string
  archivePath: string
}

export interface ArchiveJob {
  id: string
  scope: 'notes' | 'category'
  category_id: string | null
  status: string
  stage: string
  error: string
  publishedCount?: number
  failureNotification?: { status: string; stage: string; reason: string; attempt: number } | null
  notification: string
  createdAt: string
  snapshot: { category: { name: string } | null; notes: { id: string; title: string }[] }
}

export const listLibraryNotes = (offset = 0) =>
  request.get<never, { items: Task[]; total: number }>('/library/notes', {
    params: { offset, limit: 200 }, suppressToast: true,
  })
export const getLibraryNote = (id: string) => request.get<never, Task>(`/library/notes/${encodeURIComponent(id)}`)
export const deleteLibraryNote = (id: string) => request.delete(`/library/notes/${encodeURIComponent(id)}`)
export const importLibraryNotes = (tasks: Task[]) => request.post('/library/import', { tasks })
export const listCategories = () => request.get<never, Category[]>('/library/categories', { suppressToast: true })
export const createCategory = (name: string) => request.post<never, Category>('/library/categories', { name })
export const renameCategory = (id: string, name: string) => request.patch(`/library/categories/${id}`, { name })
export const deleteCategory = (id: string) => request.delete(`/library/categories/${id}`)
export const assignNotes = (noteIds: string[], categoryId: string | null) =>
  request.post('/library/membership', { note_ids: noteIds, category_id: categoryId })
export const listArchiveJobs = () => request.get<never, ArchiveJob[]>('/library/archive/jobs', { suppressToast: true })
export const createArchiveJob = (noteIds: string[], categoryId?: string, autoClassify = false) =>
  request.post<never, ArchiveJob>('/library/archive/jobs', { note_ids: noteIds, category_id: categoryId, auto_classify: autoClassify })
export const retryArchiveJob = (id: string) => request.post(`/library/archive/jobs/${id}/retry`)
export const getArchiveConfig = () =>
  request.get<never, { ready: boolean; message: string }>('/library/archive/config', { suppressToast: true })

export interface VaultTree {
  name: string
  paths: string[]
  fileCount: number
  folderCount: number
  scannedAt: string
}

export interface VaultSyncReport {
  matched: number
  categorized: number
  archived: number
  unverified: number
  unchanged: number
  skipped: number
  conflicts: { path: string; reason: string }[]
  scannedAt: string
}

export const getVaultTree = () => request.get<never, VaultTree>('/library/vault/tree', {
  timeout: 120000, suppressToast: true,
})
export const syncVault = () => request.post<never, VaultSyncReport>('/library/vault/sync', {}, {
  timeout: 300000, suppressToast: true,
})
