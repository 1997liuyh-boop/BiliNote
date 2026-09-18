import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/store/taskStore/libraryStore.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
})

// 使用实际 Zustand 状态逻辑，仅隔离网络请求和浏览器持久化。
function setup(generate = async () => ({ task_id: 'new-task' })) {
  const calls = []
  const module = { exports: {} }
  const mocks = {
    'zustand/middleware': { persist: initializer => initializer, createJSONStorage: () => undefined },
    'idb-keyval': {},
    '@/services/library': {},
    '@/services/note': { generateNote: async payload => { calls.push(payload); return generate(payload) } },
    'react-hot-toast': { default: { success() {} } },
  }
  vm.runInNewContext(outputText, {
    exports: module.exports,
    require: name => name in mocks ? mocks[name] : require(name),
  })
  const store = module.exports.useTaskStore
  const formData = {
    platform: 'youtube', video_url: 'https://www.youtube.com/watch?v=original',
    model_name: 'test', task_id: 'old-task',
  }
  store.getState().addPendingTask('old-task', 'youtube', formData)
  store.getState().updateTaskContent('old-task', {
    status: 'FAILED', errorMessage: '旧错误', markdown: '原笔记',
    archiveStatus: 'ARCHIVED', archivePath: '原分类/原笔记.md',
  })
  return { store, calls, formData }
}

test('来源不变时复用原任务，清除错误并保留已有笔记', async () => {
  const { store, calls, formData } = setup()
  await store.getState().retryTask('old-task', { ...formData, style: 'detailed' })
  assert.equal(calls[0].task_id, 'old-task')
  const task = store.getState().getCurrentTask()
  assert.equal(task.status, 'PENDING')
  assert.equal(task.errorMessage, '')
  assert.equal(task.markdown, '原笔记')
  assert.equal(task.formData.style, 'detailed')
  assert.equal(store.getState().tasks.length, 1)
})

for (const [name, changes] of [
  ['更换平台', { platform: 'bilibili' }],
  ['更换链接', { video_url: 'https://www.youtube.com/watch?v=other' }],
  ['改用本地上传', { platform: 'local', video_url: '/uploads/video.mp4' }],
]) {
  test(`${name}时创建独立任务，不覆盖旧笔记和入库状态`, async () => {
    const { store, calls, formData } = setup()
    const oldTask = store.getState().getCurrentTask()
    await store.getState().retryTask('old-task', { ...formData, ...changes })
    assert.equal(calls[0].task_id, '')
    assert.equal(calls[0].platform, changes.platform || formData.platform)
    assert.equal(calls[0].video_url, changes.video_url || formData.video_url)
    const state = store.getState()
    assert.equal(state.tasks.length, 2)
    assert.equal(state.tasks.find(task => task.id === 'old-task'), oldTask)
    const task = state.getCurrentTask()
    assert.equal(task.id, 'new-task')
    assert.equal(task.formData.task_id, 'new-task')
    assert.equal(task.platform, calls[0].platform)
    assert.equal(task.audioMeta.platform, calls[0].platform)
    assert.equal(task.transcript.full_text, '')
    assert.equal(task.markdown.length, 0)
    assert.equal(task.archiveStatus, 'UNARCHIVED')
  })
}

for (const sourceChanged of [false, true]) {
  test(`请求失败时不改变原任务${sourceChanged ? '（更换来源）' : '（原来源）'}`, async () => {
    const { store, formData } = setup(async () => { throw new Error('network failure') })
    const before = store.getState()
    await assert.rejects(store.getState().retryTask('old-task', {
      ...formData, ...(sourceChanged ? { platform: 'local' } : {}),
    }), /network failure/)
    assert.equal(store.getState(), before)
  })
}

test('不传新表单时沿用原平台和链接', async () => {
  const { store, calls, formData } = setup()
  await store.getState().retryTask('old-task')
  assert.equal(calls[0].platform, formData.platform)
  assert.equal(calls[0].video_url, formData.video_url)
  assert.equal(calls[0].task_id, 'old-task')
})
