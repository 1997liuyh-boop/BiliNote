import request from '@/utils/request'
import type { NoteFormValues } from '@/pages/HomePage/components/NoteForm'

export interface Episode {
  page: number
  cid: number
  title: string
  duration: number
  url: string
}

export interface VideoCollection {
  bvid: string
  title: string
  kind: 'multipart'
  currentPage: number
  total: number
  episodes: Episode[]
}

export type GenerationOptions = NoteFormValues & { video_url: string; provider_id: string }
export type CollectionSubmission = GenerationOptions & { request_id: string; pages: number[] }
export interface CollectionResult {
  created: boolean
  total: number
  tasks: { task_id: string; page: number; title: string; url: string }[]
}

export const getVideoCollection = (videoUrl: string, signal: AbortSignal) =>
  request.get<never, VideoCollection>('/video/collection', {
    params: { video_url: videoUrl }, signal, timeout: 60000, suppressToast: true,
  })

export const generateCollection = (data: CollectionSubmission) =>
  request.post<never, CollectionResult>('/generate_collection', data, {
    timeout: 60000, suppressToast: true,
  })
