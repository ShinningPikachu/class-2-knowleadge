/**
 * Expose the running DeepSeek Harness agent to the native Class Knowledge app.
 *
 * DSH has no public local HTTP/CLI surface, so the plugin owns a private Unix
 * socket in ~/.dsh. Requests and responses are newline-delimited JSON. A ping
 * is answered locally; it never creates a model session.
 */
import { randomUUID } from 'node:crypto'
import { chmodSync, existsSync, lstatSync, mkdirSync, unlinkSync } from 'node:fs'
import { createConnection, createServer } from 'node:net'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const SOCKET = process.env.DSH_CLASS_AGENT_SOCKET ||
  join(homedir(), '.dsh', 'class-knowledge-agent.sock')

const REQUEST_TIMEOUT_MS = Math.max(1_000, Number(process.env.DSH_CLASS_AGENT_TIMEOUT_MS) || 180_000)
const MAX_FRAME_BYTES = 1_048_576

function timeoutError() {
  return new Error('DSH agent timed out')
}

function beforeDeadline(promise, deadline) {
  const remaining = deadline - Date.now()
  if (remaining <= 0) return Promise.reject(timeoutError())

  let timer
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(timeoutError()), remaining)
    timer.unref?.()
  })
  return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer))
}

/** Read the last complete assistant text added after firstSeq. */
function summarize(session, firstSeq) {
  let started = false
  let text = ''
  for (let seq = firstSeq; seq < session.seq; seq += 1) {
    const event = session.eventAt(seq)
    if (!event) continue
    if (event.type === 'turn/start') { started = true; continue }
    if (!started || event.type !== 'assistant/message') continue
    const joined = (event.data?.message?.content || [])
      .filter(block => block.type === 'text')
      .map(block => block.text)
      .join('')
    if (joined) text = joined
  }
  return text
}

/** DSH freezes user messages after their id and source have been assigned. */
function userMessage(text) {
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'user' },
  })
}

export async function askHarness(ctx, prompt, system) {
  if (!prompt.trim()) throw new Error('prompt is required')

  const agents = ctx.agents || ctx.get?.('agents', false)
  const defaultModel = ctx.get?.('agentDefaultModel', false)
  const subagents = ctx.get?.('subagents', false)
  if (!agents || !defaultModel) throw new Error('DSH agents service unavailable')

  const selection = defaultModel.currentSelection?.() || {}
  const root = process.env.CLASS2KNOWLEDGE_ROOT || process.cwd()
  const deadline = Date.now() + REQUEST_TIMEOUT_MS
  const creating = Promise.resolve().then(() => agents.create({
    sessionId: `classlib-${randomUUID()}`,
    meta: { cwd: root },
    agentOptions: {
      ...(selection.provider ? { provider: selection.provider } : {}),
      ...(selection.model ? { model: selection.model } : {}),
    },
  }))
  let handle
  try {
    handle = await beforeDeadline(creating, deadline)
  } catch (error) {
    // If creation finishes after its deadline, still release its owned handle.
    void creating.then(lateHandle => lateHandle?.dispose?.()).catch(() => {})
    throw error
  }

  try {
    const agent = handle?.agent
    if (!agent) throw new Error('failed to create DSH agent')
    if (!agent.session || typeof agent.whenIdle !== 'function' || typeof agent.followup !== 'function') {
      throw new Error('DSH agent returned an unsupported handle')
    }

    const firstSeq = agent.session.seq
    const content = system ? `${system}\n\n---\n\n${prompt}` : prompt
    await beforeDeadline(Promise.resolve(agent.followup(userMessage(content))), deadline)

    for (;;) {
      await beforeDeadline(agent.whenIdle(), deadline)
      const idleSeq = agent.session.seq
      if (subagents?.waitForChildren) {
        try {
          const hasRunningChildren = await beforeDeadline(subagents.waitForChildren(agent), deadline)
          if (hasRunningChildren) continue
        } catch (error) {
          if (error?.message === 'DSH agent timed out') throw error
          // Older DSH builds may not expose the subagent service to plugins.
        }
      }
      if (agent.status === 'idle' && agent.session.seq === idleSeq) break
    }

    const answer = summarize(agent.session, firstSeq)
    if (!answer.trim()) throw new Error('DSH agent returned an empty answer')
    return answer
  } finally {
    // agents.create() returns an owned handle; disposing it releases the
    // runtime while the persisted session stays available in DSH history.
    try { await handle?.dispose?.() } catch (error) {
      console.warn('[class2knowledge] could not dispose agent handle:', error?.message || error)
    }
  }
}

function send(sock, response) {
  if (sock.destroyed || !sock.writable) return
  sock.write(`${JSON.stringify(response)}\n`)
}

/** Turn one parsed socket request into a response; exported for isolated tests. */
export async function handleAgentRequest(ctx, request) {
  const id = request?.id ?? null
  if (request?.action === 'ping') return { id, ok: true }
  try {
    const answer = await askHarness(ctx, String(request?.prompt ?? ''), request?.system)
    return { id, text: answer }
  } catch (error) {
    return { id, error: String(error?.message || error) }
  }
}

function socketIsActive(path) {
  return new Promise(resolve => {
    let settled = false
    const probe = createConnection(path)
    const timer = setTimeout(() => finish(true), 500)
    timer.unref?.()

    function finish(active) {
      if (settled) return
      settled = true
      clearTimeout(timer)
      probe.destroy()
      resolve(active)
    }

    probe.once('connect', () => finish(true))
    probe.once('error', error => {
      finish(!['ECONNREFUSED', 'ENOENT'].includes(error?.code))
    })
  })
}

function sameSocket(path, identity) {
  if (!identity) return false
  try {
    const current = lstatSync(path)
    return current.isSocket() && current.dev === identity.dev && current.ino === identity.ino
  } catch {
    return false
  }
}

export function startAgentBridge(ctx) {
  let disposed = false
  let ownsSocket = false
  let socketIdentity = null

  const server = createServer(sock => {
    let buffer = ''
    sock.setEncoding('utf8')
    sock.on('data', chunk => {
      buffer += chunk
      if (Buffer.byteLength(buffer, 'utf8') > MAX_FRAME_BYTES) {
        send(sock, { id: null, error: 'request is too large' })
        sock.destroy()
        return
      }
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index).trim()
        buffer = buffer.slice(index + 1)
        if (line) void handleLine(sock, line)
      }
    })
    sock.on('error', () => {})
  })

  async function handleLine(sock, line) {
    let request
    try {
      request = JSON.parse(line)
    } catch {
      send(sock, { id: null, error: 'invalid JSON' })
      return
    }

    send(sock, await handleAgentRequest(ctx, request))
  }

  server.on('error', error => {
    console.error('[class2knowledge] agent bridge unavailable:', error?.message || error)
  })

  async function listen() {
    try {
      mkdirSync(dirname(SOCKET), { recursive: true, mode: 0o700 })
      if (existsSync(SOCKET)) {
        if (!lstatSync(SOCKET).isSocket()) {
          console.warn('[class2knowledge] refusing to replace a non-socket bridge path')
          return
        }
        if (await socketIsActive(SOCKET)) {
          console.warn('[class2knowledge] another bridge already owns the socket')
          return
        }
        unlinkSync(SOCKET)
      }
      if (disposed) return

      server.listen(SOCKET, () => {
        ownsSocket = true
        try {
          const stat = lstatSync(SOCKET)
          socketIdentity = { dev: stat.dev, ino: stat.ino }
          chmodSync(SOCKET, 0o600)
        } catch (error) {
          console.warn('[class2knowledge] could not secure agent socket:', error?.message || error)
        }
        if (disposed) closeOwnedSocket()
      })
    } catch (error) {
      console.error('[class2knowledge] agent bridge unavailable:', error?.message || error)
    }
  }

  function closeOwnedSocket() {
    if (!ownsSocket) return
    ownsSocket = false
    try { server.close() } catch { /* ignore */ }
    if (sameSocket(SOCKET, socketIdentity)) {
      try { unlinkSync(SOCKET) } catch { /* ignore */ }
    }
    socketIdentity = null
  }

  void listen()

  return () => {
    disposed = true
    closeOwnedSocket()
  }
}
