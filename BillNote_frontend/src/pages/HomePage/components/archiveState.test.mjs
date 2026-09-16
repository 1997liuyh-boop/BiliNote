import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// 只加载纯筛选函数，不启动浏览器、不访问真实服务。
const source = readFileSync(new URL('./archiveState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } })
const { getArchiveState, matchesArchiveFilter } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

for (const status of ['QUEUED', 'RUNNING']) {
  test(`${status} 属于入库中，支持有旧版本的更新任务`, () => {
    for (const archiveStatus of ['UNARCHIVED', 'ARCHIVED', 'OUTDATED']) {
      const task = { archiveStatus, archiveJob: { status, stage: 'UPLOAD' } }
      assert.equal(matchesArchiveFilter(task, 'archiving'), true)
      assert.equal(matchesArchiveFilter(task, 'failed'), false)
      assert.equal(getArchiveState(task), status)
    }
  })
}

test('入库失败不等于视频生成失败，且可以与旧版本已入库重叠', () => {
  const task = { archiveStatus: 'OUTDATED', archiveJob: { status: 'FAILED', stage: 'PUBLISH' } }
  assert.equal(matchesArchiveFilter(task, 'failed'), true)
  assert.equal(matchesArchiveFilter(task, 'archived'), true)
  assert.equal(matchesArchiveFilter(task, 'unarchived'), false)
  assert.equal(matchesArchiveFilter({ archiveStatus: 'UNARCHIVED', status: 'FAILED' }, 'failed'), false)
})

test('仅通知失败或通知重试不误报入库失败与入库中', () => {
  for (const status of ['FAILED', 'QUEUED', 'RUNNING']) {
    const task = { archiveStatus: 'ARCHIVED', archiveJob: { status, stage: 'NOTIFY' } }
    assert.equal(matchesArchiveFilter(task, 'failed'), false)
    assert.equal(matchesArchiveFilter(task, 'archiving'), false)
    assert.equal(matchesArchiveFilter(task, 'archived'), true)
    assert.equal(getArchiveState(task), 'ARCHIVED')
  }
})

test('无任务和已完成任务保持已入库、未入库语义', () => {
  for (const archiveJob of [null, { status: 'COMPLETED', stage: 'NOTIFY' }]) {
    const task = { archiveStatus: 'ARCHIVED', archiveJob }
    assert.equal(matchesArchiveFilter(task, 'archived'), true)
    assert.equal(matchesArchiveFilter(task, 'failed'), false)
    assert.equal(matchesArchiveFilter(task, 'archiving'), false)
  }
  assert.equal(matchesArchiveFilter({}, 'all'), true)
  assert.equal(matchesArchiveFilter({}, 'unarchived'), true)
})
