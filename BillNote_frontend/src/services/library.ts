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
export const createArchiveJob = (noteIds: string[], categoryId?: string) =>
  request.post<never, ArchiveJob>('/library/archive/jobs', { note_ids: noteIds, category_id: categoryId })
export const retryArchiveJob = (id: string) => request.post(`/library/archive/jobs/${id}/retry`)
export const getArchiveConfig = () =>
  request.get<never, { ready: boolean; message: string }>('/library/archive/config', { suppressToast: true })
