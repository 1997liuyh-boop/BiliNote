import type { Task } from '@/store/taskStore'

type ArchiveTask = Pick<Task, 'archiveStatus' | 'archiveJob'>

export function getArchiveState(task: ArchiveTask) {
  const job = task.archiveJob
  // 通知阶段已经完成文件入库，不能把通知失败误报为入库失败。
  if (job?.published && task.archiveStatus === 'ARCHIVED') return 'ARCHIVED'
  if (job && job.stage !== 'NOTIFY' && ['QUEUED', 'RUNNING', 'FAILED'].includes(job.status)) return job.status
  return task.archiveStatus || 'UNARCHIVED'
}

export function matchesArchiveFilter(task: ArchiveTask, filter: string) {
  const archived = task.archiveStatus === 'ARCHIVED' || task.archiveStatus === 'OUTDATED'
  const state = getArchiveState(task)
  if (filter === 'archived') return archived
  if (filter === 'unarchived') return !archived
  if (filter === 'archiving') return state === 'QUEUED' || state === 'RUNNING'
  if (filter === 'failed') return state === 'FAILED'
  return true
}
