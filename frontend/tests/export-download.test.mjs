import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError } from '../src/api.ts'
import {signIn,tick} from './tab-environment.mjs'
import {readSession} from '../src/session.ts'
import { authSession } from '../src/session.ts'
import { fetchExportArchive, startArchiveDownload, MAX_ARCHIVE_BYTES } from '../src/features/exports/download.ts'

const owner = 'synthetic-export-owner'
const archiveResponse = (body = 'PK synthetic ZIP', headers = {}) => new Response(body, { headers: {
  'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="ai-notes-export-20261020T123456Z.zip"', ...headers,
} })
function environment() {
  const previous = { fetch: globalThis.fetch, document: globalThis.document, create: URL.createObjectURL, revoke: URL.revokeObjectURL }
  signIn(owner,'a')
  return { restore() { globalThis.fetch = previous.fetch; globalThis.document = previous.document; URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke } }
}

test('complete export uses tab Bearer and CSRF in one authenticated POST, receives bounded chunks and a safe filename', async () => {
  const env = environment(), requests = [], progress = [], chunks = [new Uint8Array([80, 75]), new Uint8Array([3, 4, 5])]
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return archiveResponse(new ReadableStream({ start(stream) { for (const chunk of chunks) stream.enqueue(chunk); stream.close() } }), { 'Content-Length': '5' })
  }
  try {
    const result = await fetchExportArchive(owner, new AbortController().signal, value => progress.push(value))
    assert.deepEqual(requests.map(item => item.url), ['/api/v1/exports/archive'])
    assert.equal(requests[0].options.method, 'POST'); assert.equal(requests[0].options.credentials, 'omit')
    assert.equal(requests[0].options.headers['X-CSRF-Token'], readSession().csrf_token); assert.equal(requests[0].options.body, '{}')
    assert.equal(result.filename, 'ai-notes-export-20261020T123456Z.zip')
    assert.deepEqual([...new Uint8Array(await result.blob.arrayBuffer())], [80, 75, 3, 4, 5])
    assert.deepEqual(progress.at(-1), { phase: 'downloading', received: 5, total: 5 })
    assert.equal(requests[0].options.signal.aborted, true)
  } finally { env.restore() }
})

test('export discards late headers after a tab account replacement', async () => {
  const env = environment(); let resolve, count = 0, requestSignal
  globalThis.fetch = (_url, options) => { count++; requestSignal = options.signal; return new Promise(yes => { resolve = yes }) }
  try {
    const pending = fetchExportArchive(owner, new AbortController().signal)
    await tick()
    authSession.invalidate(authSession.ticket()); authSession.login('new-export-owner', authSession.ticket())
    assert.equal(requestSignal.aborted, true)
    resolve(archiveResponse())
    await assert.rejects(pending, { name: 'AbortError' }); assert.equal(count, 1)
  } finally { env.restore() }
})

test('cancelling an export aborts transport and discards late export headers', async () => {
  const env = environment(), controller = new AbortController(); let resolve, count = 0, requestSignal
  globalThis.fetch = (_url, options) => { count++; requestSignal = options.signal; return new Promise(yes => { resolve = yes }) }
  try {
    const pending = fetchExportArchive(owner, controller.signal)
    await tick()
    controller.abort(); assert.equal(requestSignal.aborted, true); resolve(archiveResponse())
    await assert.rejects(pending, { name: 'AbortError' }); assert.equal(count, 1)
  } finally { env.restore() }
})

test('logout during stream reception rejects late chunks, cancels the reader and stops progress', async () => {
  const env = environment(), progress = []; let resolve, cancelled = false, released = false, reads = 0, count = 0
  globalThis.fetch = async () => ({
    ok: true, headers: new Headers({ 'Content-Type': 'application/zip' }),
    body: { getReader: () => ({ read: () => { reads++; return new Promise(yes => { resolve = yes }) }, cancel: async () => { cancelled = true }, releaseLock: () => { released = true } }) },
  })
  try {
    const pending = fetchExportArchive(owner, new AbortController().signal, value => progress.push(value))
    for (let i = 0; i < 20 && !reads; i++) await tick()
    assert.equal(reads, 1)
    const before = progress.length
    authSession.invalidate(authSession.ticket()); authSession.login('other-export-owner', authSession.ticket())
    resolve({ value: new Uint8Array([80, 75]), done: false })
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(progress.length, before); assert.equal(cancelled, true); assert.equal(released, true)
  } finally { env.restore() }
})

test('server export errors preserve their request ID and message', async () => {
  const env = environment(); let count = 0
  globalThis.fetch = async () => new Response(JSON.stringify({ code: 'export_busy', message: '已有导出正在生成，请稍后重试。', request_id: 'synthetic-request-id' }), { status: 409 })
  try {
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), failure => {
      assert.ok(failure instanceof ApiError); assert.equal(failure.code, 'export_busy'); assert.equal(failure.status, 409)
      assert.equal(failure.requestId, 'synthetic-request-id'); assert.match(failure.message, /已有导出/); return true
    })
    assert.equal(authSession.snapshot().owner, owner)
  } finally { env.restore() }
})

test('a current unauthorized export locks its session', async () => {
  const env = environment(); let count = 0
  globalThis.fetch = async () => new Response(JSON.stringify({ code: 'unauthorized', message: '登录已过期。' }), { status: 401 })
  try {
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), { name: 'AbortError' })
    assert.equal(authSession.snapshot().phase, 'anonymous')
  } finally { env.restore() }
})

test('a late unauthorized export cannot sign out a subsequent account', async () => {
  const env = environment(); let count = 0, resolve
  globalThis.fetch = async () => new Promise(yes => { resolve = yes })
  try {
    const pending = fetchExportArchive(owner, new AbortController().signal)
    for (let i = 0; i < 20 && !resolve; i++) await tick()
    assert.ok(resolve)
    authSession.invalidate(authSession.ticket()); authSession.login('next-export-owner', authSession.ticket())
    resolve(new Response('{}', { status: 401 }))
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(authSession.snapshot().phase, 'active'); assert.equal(authSession.snapshot().owner, 'next-export-owner')
  } finally { env.restore() }
})

test('oversized Content-Length is rejected before reading and cancels the response', async () => {
  const env = environment(); let count = 0, read = false, cancelled = false
  globalThis.fetch = async () => ({
    ok: true, headers: new Headers({ 'Content-Type': 'application/zip', 'Content-Length': String(MAX_ARCHIVE_BYTES + 1) }),
    body: { cancel: async () => { cancelled = true }, getReader: () => { read = true; throw new Error('should not read') } },
  })
  try {
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), /超过上限/)
    assert.equal(read, false); assert.equal(cancelled, true)
  } finally { env.restore() }
})

test('chunked responses have the same limit even without Content-Length', async () => {
  const env = environment(); let count = 0, cancelled = false
  globalThis.fetch = async () => ({
    ok: true, headers: new Headers({ 'Content-Type': 'application/zip' }),
    body: { getReader: () => ({ read: async () => ({ done: false, value: { byteLength: MAX_ARCHIVE_BYTES + 1 } }), cancel: async () => { cancelled = true }, releaseLock() {} }) },
  })
  try {
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), /已停止接收/)
    assert.equal(cancelled, true)
  } finally { env.restore() }
})

test('non-ZIP or incomplete responses cannot be handed to the browser', async () => {
  const env = environment(); let count = 0
  try {
    globalThis.fetch = async () => new Response('<html>error</html>', { headers: { 'Content-Type': 'text/html' } })
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), /未返回 ZIP/)
    count = 0
    globalThis.fetch = async () => archiveResponse('PK', { 'Content-Length': '3' })
    await assert.rejects(fetchExportArchive(owner, new AbortController().signal), /接收不完整/)
  } finally { env.restore() }
})

test('an unsafe server filename falls back to the fixed archive name', async () => {
  const env = environment(); let count = 0
  globalThis.fetch = async () => archiveResponse('PK', { 'Content-Disposition': 'attachment; filename="../../private.zip"' })
  try { assert.equal((await fetchExportArchive(owner, new AbortController().signal)).filename, 'ai-notes-export.zip') }
  finally { env.restore() }
})

function downloadEnvironment(onAppend = () => {}) {
  const created = [], revoked = [], clicks = [], removed = []
  URL.createObjectURL = blob => { created.push(blob); return 'blob:synthetic-export' }
  URL.revokeObjectURL = url => revoked.push(url)
  globalThis.document = { createElement: () => ({ click() { clicks.push(this.download) }, remove() { removed.push(true) } }), body: { appendChild: onAppend } }
  return { created, revoked, clicks, removed }
}

test('browser handoff clicks the fixed filename and returns URL cleanup', () => {
  const env = environment(), dom = downloadEnvironment()
  try {
    const release = startArchiveDownload({ blob: new Blob(['PK']), filename: 'ai-notes-export.zip', ticket: authSession.ticket(), owner }, new AbortController().signal)
    assert.equal(dom.created.length, 1); assert.deepEqual(dom.clicks, ['ai-notes-export.zip']); assert.equal(dom.removed.length, 1)
    release(); assert.deepEqual(dom.revoked, ['blob:synthetic-export'])
  } finally { env.restore() }
})

test('a completed archive cannot trigger a download after logout, including same-owner relogin', () => {
  const env = environment(), dom = downloadEnvironment()
  try {
    const archive = { blob: new Blob(['PK']), filename: 'ai-notes-export.zip', ticket: authSession.ticket(), owner }
    authSession.invalidate(authSession.ticket()); authSession.login(owner, authSession.ticket())
    assert.throws(() => startArchiveDownload(archive, new AbortController().signal), { name: 'AbortError' })
    assert.equal(dom.created.length, 0); assert.equal(dom.clicks.length, 0)
  } finally { env.restore() }
})

test('a cancelled or newly invalidated handoff never clicks and revokes any created URL', () => {
  const env = environment(), controller = new AbortController()
  let dom = downloadEnvironment()
  try {
    const archive = { blob: new Blob(['PK']), filename: 'ai-notes-export.zip', ticket: authSession.ticket(), owner }
    controller.abort()
    assert.throws(() => startArchiveDownload(archive, controller.signal), { name: 'AbortError' })
    assert.equal(dom.created.length, 0)
    dom = downloadEnvironment(() => authSession.invalidate(authSession.ticket()))
    assert.throws(() => startArchiveDownload(archive, new AbortController().signal), { name: 'AbortError' })
    assert.equal(dom.clicks.length, 0); assert.deepEqual(dom.revoked, ['blob:synthetic-export']); assert.equal(dom.removed.length, 1)
  } finally { env.restore() }
})
