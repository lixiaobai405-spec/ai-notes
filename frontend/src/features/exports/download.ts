import { ApiError,authContext,rejectExpiredSession } from '../../api.ts'
import { authSession, SessionChangedError } from '../../session.ts'
import type { SessionTicket } from '../../session.ts'

// The server caps payloads at 512 MiB and entries at 50,002. Reserve 16 MiB for
// ZIP headers and DEFLATE overhead even when the archive has many small files.
export const MAX_ARCHIVE_BYTES = 528 * 1024 * 1024
const REQUEST_TIMEOUT_MS = 180000
const MAX_JSON_BYTES = 64 * 1024
export type ExportProgress = { phase: 'preparing' | 'downloading'; received: number; total?: number }
export type ExportArchive = { blob: Blob; filename: string; ticket: SessionTicket; owner: string }

function ensureSession(ticket: SessionTicket, owner: string, signal: AbortSignal) {
  if (!authSession.current(ticket, owner)) throw new SessionChangedError()
  signal.throwIfAborted()
}

async function readBody(response: Response, limit: number, check: () => void, progress?: (received: number, total?: number) => void) {
  const rejectBody = (message: string): never => { void response.body?.cancel().catch(() => {}); throw new Error(message) }
  const length = response.headers.get('Content-Length')
  const total = length === null ? undefined : Number(length)
  if (total !== undefined && (!/^\d+$/.test(length!) || !Number.isSafeInteger(total))) rejectBody('导出服务返回无效的文件大小，请重试。')
  if (total !== undefined && total > limit) rejectBody('当前导出大小超过上限，请联系管理员处理。')
  if (!response.body) throw new Error('导出服务未返回文件内容，请重试。')
  const reader = response.body.getReader(), parts: BlobPart[] = []
  let received = 0, finished = false
  try {
    while (true) {
      check()
      const item = await reader.read()
      check()
      if (item.done) { finished = true; break }
      received += item.value.byteLength
      if (received > limit) throw new Error('导出文件超过浏览器下载上限，已停止接收。')
      parts.push(item.value)
      progress?.(received, total)
    }
    if (total !== undefined && received !== total) throw new Error('导出文件接收不完整，请重新下载。')
    check()
    return new Blob(parts)
  } finally {
    if (!finished) void reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

async function errorResponse(response: Response, check: () => void) {
  const requestId = response.headers.get('X-Request-ID') || ''
  try {
    const body = await readBody(response, MAX_JSON_BYTES, check)
    check()
    const text = await body.text()
    check()
    const data = JSON.parse(text)
    return new ApiError(response.status, typeof data.code === 'string' ? data.code : 'export_failed',
      typeof data.message === 'string' ? data.message : '完整导出失败，请稍后重试。',
      typeof data.request_id === 'string' ? data.request_id : requestId)
  } catch (failure) {
    check()
    if (failure instanceof SessionChangedError) throw failure
    return new ApiError(response.status, 'export_failed', response.status === 401 ? '登录已过期，请重新登录。' : '完整导出失败，请稍后重试。', requestId)
  }
}

export async function fetchExportArchive(owner: string, signal: AbortSignal, progress: (value: ExportProgress) => void = () => {}): Promise<ExportArchive> {
  const ticket = authSession.ticket(), controller = new AbortController()
  let timedOut = false
  const check = () => {
    ensureSession(ticket, owner, signal)
    if (timedOut) throw new Error('导出或下载超时，请稍后重试。')
    controller.signal.throwIfAborted()
  }
  check()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  const unsubscribe = authSession.subscribe(() => { if (!authSession.current(ticket, owner)) controller.abort() })
  const timeout = setTimeout(() => { timedOut = true; controller.abort() }, REQUEST_TIMEOUT_MS)
  try {
    progress({ phase: 'preparing', received: 0 })
    const context=await authContext()
    check()
    const response = await fetch('/api/v1/exports/archive', {
      method: 'POST', credentials: 'omit', redirect:'error', signal: controller.signal,
      headers: { ...context.headers, 'Content-Type': 'application/json' }, body: '{}',
    })
    check()
    if (!response.ok) {
      const failure = await errorResponse(response, check)
      check()
      rejectExpiredSession(response,context.session)
      throw failure
    }
    if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/zip') {
      void response.body?.cancel().catch(() => {})
      throw new Error('导出服务未返回 ZIP 文件，请重试。')
    }
    progress({ phase: 'downloading', received: 0 })
    const blob = await readBody(response, MAX_ARCHIVE_BYTES, check, (received, total) => {
      check(); progress({ phase: 'downloading', received, total })
    })
    check()
    if (!blob.size) throw new Error('导出文件为空，请重试。')
    const filename = response.headers.get('Content-Disposition')?.match(/(?:^|;)\s*filename="(ai-notes-export-\d{8}T\d{6}Z\.zip)"(?:;|$)/i)?.[1] || 'ai-notes-export.zip'
    return { blob, filename, ticket, owner }
  } catch (failure) {
    check()
    if (failure instanceof TypeError) throw new Error('网络连接中断，完整导出尚未下载完成，请重试。')
    throw failure
  } finally {
    controller.abort(); clearTimeout(timeout); unsubscribe(); signal.removeEventListener('abort', abort)
  }
}

// Recheck immediately before creating a private object URL and handing it off.
// The caller revokes this URL after handoff or when the component unmounts.
export function startArchiveDownload(archive: ExportArchive, signal: AbortSignal) {
  ensureSession(archive.ticket, archive.owner, signal)
  const url = URL.createObjectURL(archive.blob), link = document.createElement('a')
  const release = () => URL.revokeObjectURL(url)
  try {
    ensureSession(archive.ticket, archive.owner, signal)
    link.href = url; link.download = archive.filename
    document.body.appendChild(link)
    ensureSession(archive.ticket, archive.owner, signal)
    link.click()
    return release
  } catch (failure) { release(); throw failure }
  finally { link.remove() }
}
