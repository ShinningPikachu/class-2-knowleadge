import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { Readable } from 'node:stream'
import { tmpdir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import test from 'node:test'

const scratch = mkdtempSync(join(tmpdir(), 'class2knowledge-plugin-'))
const workspace = join(scratch, 'workspace')
const database = join(workspace, 'library', 'library.sqlite3')
const jobDatabase = join(workspace, 'jobs', 'jobs.sqlite3')

mkdirSync(join(workspace, 'library'), { recursive: true })
mkdirSync(join(workspace, 'jobs'), { recursive: true })
process.env.CLASS2KNOWLEDGE_ROOT = workspace
process.env.CLASS2KNOWLEDGE_APP_BIN = join(scratch, 'missing', 'ClassKnowledgeApp')
process.env.CLASS2KNOWLEDGE_APP_PATH = join(scratch, 'missing', 'Class Knowledge.app')
process.env.DSH_CLASS_AGENT_TIMEOUT_MS = '2000'

execFileSync('sqlite3', [database], {
  input: [
    'create table subjects (id text primary key, name text not null);',
    'create table folders (id text primary key, subject_id text not null, name text not null, created_at text);',
    'create table documents (id text primary key, subject_id text not null, original_name text not null, status text, media_type text, size_bytes integer default 0, folder_id text, created_at text);',
    'create table document_chunks (id text primary key, document_id text not null, ordinal integer, locator text, content text);',
    "insert into subjects values ('subject-1', 'Speech Science');",
    "insert into folders values ('folder-1', 'subject-1', 'Week 1', '2026-10-29');",
    "insert into documents values ('document-1', 'subject-1', 'Lecture Notes.pdf', 'indexed', 'application/pdf', 1200, null, '2026-10-29');",
    "insert into documents values ('document-2', 'subject-1', 'Recording Notes.txt', 'indexed', 'text/plain', 80, 'folder-1', '2026-10-29');",
    "insert into document_chunks values ('chunk-1', 'document-1', 1, 'page 3', 'literal 100% and token x_y; source-backed result');",
    "insert into document_chunks values ('chunk-2', 'document-2', 1, 'line 1', 'a recording note');",
    ''
  ].join('\n')
})

execFileSync('sqlite3', [jobDatabase], {
  input: [
    'create table jobs (id text, kind text, title text, status text, priority integer, stage text, progress integer, message text, error text, created_at text, updated_at text, completed_at text);',
    "insert into jobs values ('job-1', 'lecture', 'Lecture 1', 'queued', 50, 'queued', 0, 'Waiting', '', '2026-10-29T08:00:00Z', '2026-10-29T08:00:00Z', '');",
    "insert into jobs values ('job-2', 'lecture', 'Lecture 2', 'completed', 50, 'complete', 100, 'Done', '', '2026-10-28T08:00:00Z', '2026-10-28T09:00:00Z', '2026-10-28T09:00:00Z');",
    ''
  ].join('\n')
})

const { apply, buildTools, handle } = await import('../lib/index.js')
const { handleAgentRequest } = await import('../lib/agent-bridge.js')

let createdAgents = 0
let disposedAgents = 0
let lastFollowup = null
const tools = new Map()
const routes = []

function makeContext({ withAgents = false } = {}) {
  return {
    tools: {
      register(tool) {
        tools.set(tool.name, tool)
        return () => tools.delete(tool.name)
      }
    },
    webServer: {
      register(route) {
        routes.push(route)
        return () => routes.splice(routes.indexOf(route), 1)
      }
    },
    get(key) {
      if (key === 'agents' && withAgents) {
        return {
          async create() {
            createdAgents += 1
            const session = {
              seq: 0,
              events: [],
              eventAt(index) { return this.events[index] }
            }
            const agent = {
              session,
              status: 'idle',
              followup(message) {
                lastFollowup = message
                session.events.push(
                  { type: 'turn/start' },
                  { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'Local answer with source [S1].' }] } } }
                )
                session.seq = session.events.length
              },
              async whenIdle() {}
            }
            return { agent, async dispose() { disposedAgents += 1 } }
          }
        }
      }
      if (key === 'agentDefaultModel') return { currentSelection: () => ({ provider: 'test', model: 'test-model' }) }
      if (key === 'subagents') return undefined
      return undefined
    }
  }
}

test('search and document APIs use the configured workspace and literal LIKE terms', async () => {
  const subjects = await handle('subjects')
  assert.deepEqual(subjects.subjects, [{ subject: 'Speech Science', documents: 2 }])

  const docs = await handle('documents', { subject: 'Speech Science' })
  assert.equal(docs.documents.length, 2)
  const rootDocs = await handle('documents', { subject: 'Speech Science', folderId: 'root' })
  assert.deepEqual(rootDocs.documents.map(item => item.id), ['document-1'])
  const folderDocs = await handle('documents', { subject: 'Speech Science', folderId: 'folder-1' })
  assert.deepEqual(folderDocs.documents.map(item => item.id), ['document-2'])
  const folders = await handle('folders', { subject: 'Speech Science' })
  assert.deepEqual(folders.folders, [{ id: 'folder-1', name: 'Week 1', documentCount: 1 }])

  const percent = await handle('search', { query: '%', limit: 100 })
  assert.equal(percent.hits.length, 1)
  assert.equal(percent.hits[0].id, 'document-1')

  const underscore = await handle('search', { query: 'x_y' })
  assert.equal(underscore.hits.length, 1)
  assert.equal(underscore.hits[0].locator, 'page 3')

  const body = await handle('document', { id: 'document-1' })
  assert.equal(body.parts[0].content, 'literal 100% and token x_y; source-backed result')
  const jobs = await handle('jobs')
  assert.equal(jobs.jobs.length, 2)
  assert.equal(jobs.counts.queued, 1)
  assert.equal(jobs.counts.completed, 1)
  await assert.rejects(() => handle('document', { id: 'missing' }), /文档不存在/)
  await assert.rejects(() => handle('search', { query: 'x'.repeat(501) }), /too long/)
})

test('plugin registers DSH tools and a same-origin panel route', async () => {
  const registered = buildTools().map(tool => tool.name)
  assert.deepEqual(registered, ['classlib_status', 'classlib_subjects', 'classlib_jobs', 'classlib_search', 'classlib_start', 'classlib_stop'])

  const context = makeContext()
  const effects = []
  context.effect = (callback, label) => {
    assert.match(label, /tools, panel route, and agent bridge/)
    effects.push(callback())
  }
  apply(context)
  assert.equal(routes.length, 1)
  assert.equal(routes[0].path, '/api/class2knowledge')
  assert.equal(tools.size, 6)

  const status = await tools.get('classlib_status').execute()
  assert.equal(status.libraryAvailable, true)
  assert.equal(status.subjectCount, 1)
  assert.equal(status.documents, 2)
  assert.equal(status.appInstalled, false)
  assert.deepEqual(status.queueCounts, { queued: 1, running: 0, waiting: 0, deferred: 0, completed: 1, failed: 0, cancelled: 0 })
  await assert.rejects(() => tools.get('classlib_start').execute(), /找不到原生应用/)
  assert.deepEqual(await tools.get('classlib_stop').execute(), { stopped: false, wasRunning: false })

  const response = {
    code: null, headers: null, body: '',
    writeHead(code, headers) { this.code = code; this.headers = headers },
    end(body) { this.body = body }
  }
  await routes[0].handler(
    Readable.from([Buffer.from(JSON.stringify({ endpoint: 'subjects', payload: {} }))], { objectMode: false }),
    response
  )
  assert.equal(response.code, 200)
  assert.equal(JSON.parse(response.body).subjects.length, 1)

  const methodResponse = { ...response, code: null, body: '' }
  await routes[0].handler({ method: 'GET', async *[Symbol.asyncIterator]() {} }, methodResponse)
  assert.equal(methodResponse.code, 405)

  effects.pop()()
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(tools.size, 0)
  assert.equal(routes.length, 0)

  apply(context)
  assert.equal(routes.length, 1)
  assert.equal(tools.size, 6)
  effects.pop()()
})

test('start activates the native app even when its process is already running', async () => {
  const commandDir = join(scratch, 'commands')
  const appPath = process.env.CLASS2KNOWLEDGE_APP_PATH
  const appBin = process.env.CLASS2KNOWLEDGE_APP_BIN
  const openLog = join(scratch, 'open-arguments.txt')
  mkdirSync(commandDir, { recursive: true })
  mkdirSync(dirname(appBin), { recursive: true })
  mkdirSync(appPath, { recursive: true })
  writeFileSync(appBin, '')
  writeFileSync(join(commandDir, 'pgrep'), '#!/bin/sh\nprintf "43210\\n"\n')
  writeFileSync(join(commandDir, 'open'), '#!/bin/sh\nprintf "%s\\n" "$@" > "$CLASS2KNOWLEDGE_OPEN_LOG"\n')
  chmodSync(join(commandDir, 'pgrep'), 0o755)
  chmodSync(join(commandDir, 'open'), 0o755)

  const original = {
    path: process.env.PATH,
    openLog: process.env.CLASS2KNOWLEDGE_OPEN_LOG
  }
  process.env.PATH = `${commandDir}${delimiter}${original.path || ''}`
  process.env.CLASS2KNOWLEDGE_OPEN_LOG = openLog
  try {
    assert.deepEqual(await handle('start'), { running: true, started: false, activated: true, app: 'Class Knowledge' })
    assert.equal(readFileSync(openLog, 'utf8'), `-a\n${appPath}\n`)
  } finally {
    process.env.PATH = original.path
    if (original.openLog === undefined) delete process.env.CLASS2KNOWLEDGE_OPEN_LOG
    else process.env.CLASS2KNOWLEDGE_OPEN_LOG = original.openLog
  }
})

test('study chat retrieves scoped passages, keeps short conversation history, and cites sources', async () => {
  const context = makeContext({ withAgents: true })
  const result = await handle('ask', {
    question: 'What is the source-backed result?',
    subject: 'Speech Science',
    history: [{ role: 'user', content: 'I am reviewing this lecture.', context: 'Subject · Speech Science' }]
  }, context)

  assert.equal(result.answer, 'Local answer with source [S1].')
  assert.equal(result.scope, 'subject: Speech Science')
  assert.equal(result.sources.length, 1)
  assert.equal(result.sources[0].doc, 'Lecture Notes.pdf')
  assert.equal(result.sources[0].locator, 'page 3')
  assert.match(lastFollowup.content[0].text, /Conversation so far:/)
  assert.match(lastFollowup.content[0].text, /literal 100% and token x_y/)
  assert.match(lastFollowup.content[0].text, /You may also use your own subject knowledge/)
  assert.match(lastFollowup.content[0].text, /do not use Markdown tables/)

  const documentResult = await handle('ask', {
    question: 'What is in the recording note?', documentId: 'document-2'
  }, context)
  assert.equal(documentResult.scope, 'selected document: Recording Notes.txt')
  assert.deepEqual(documentResult.sources.map(source => source.id), ['document-2'])
  await assert.rejects(() => handle('ask', { question: 'Explain this', documentId: 'missing' }, context), /Selected document is unavailable/)
})

test('agent bridge answers health checks without a model turn and disposes DSH handles', async () => {
  const context = makeContext({ withAgents: true })
  const createdBefore = createdAgents
  const disposedBefore = disposedAgents
  const ping = await handleAgentRequest(context, { id: 'ping-1', action: 'ping' })
  assert.deepEqual(ping, { id: 'ping-1', ok: true })
  assert.equal(createdAgents, createdBefore)

  const answer = await handleAgentRequest(context, { id: 'ask-1', prompt: 'What does the source say?', system: 'Use only sources.' })
  assert.deepEqual(answer, { id: 'ask-1', text: 'Local answer with source [S1].' })
  assert.equal(createdAgents, createdBefore + 1)
  assert.equal(disposedAgents, disposedBefore + 1)
  assert.match(lastFollowup.content[0].text, /Use only sources/)

  assert.deepEqual(await handleAgentRequest(context, { id: 'empty', prompt: '' }), { id: 'empty', error: 'prompt is required' })
})

test.after(() => {
  rmSync(scratch, { recursive: true, force: true })
})
