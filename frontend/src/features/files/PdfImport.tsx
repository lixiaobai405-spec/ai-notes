import { createUuid } from '../../uuid'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { NoteError } from '../notes/NoteError'
import type { Note } from '../notes/types'
import { sendContent } from './upload'
import { readPrivateFile } from './download'
import { authSession,SessionChangedError } from '../../session.ts'
import type { PdfImportOut } from './types'
import './files.css'

const labels: Record<string, string> = {
  uploading: '等待上传 PDF', expired: '上传已过期，可重新选择文件继续', queued: '等待后台转换',
  processing: '正在逐页转换', ready: '全部页面已可阅读', failed: '转换中断，可继续重试', rejected: 'PDF 未能完整转换',
}
const errors: Record<string, string> = {
  pdf_password_required: 'PDF 需要密码，请先在本机解锁后重新导入。',
  pdf_invalid: 'PDF 已损坏或格式不受支持，请检查原文件。',
  pdf_page_limit: 'PDF 页数超过上限，请拆分为每份不超过 200 页后导入。',
  pdf_page_dimensions: 'PDF 包含尺寸异常的页面，请调整后重新导入。',
  pdf_page_pixels: '页面像素超过处理上限，请调整后重新导入。',
  pdf_page_bytes: '转换后的单页文件过大，请调整后重新导入。',
  storage_quota: '文件容量达到上限，已完成的页面会保留。',
  pdf_render_timeout: '页面转换超时，可重试未完成的页面。',
  pdf_renderer_failed: '页面转换中断，可重试未完成的页面。',
  pdf_note_unavailable: '笔记或账户状态已变化，请恢复后重试。',
}

function validPdf(file: File) {
  if (!file.name.toLowerCase().endsWith('.pdf') || !file.size || file.size > 50 * 1024 * 1024 || file.name.length > 200)
    throw new Error('请选择大于 0、不超过 50 MiB，且文件名不超过 200 个字符的 PDF。')
}

async function transfer(item: PdfImportOut, file: File, progress: (percent: number) => void, signal: AbortSignal) {
  validPdf(file)
  if (file.name !== item.filename) throw new Error('请选择原来导入的同名 PDF 文件。')
  if (item.upload.status !== 'completed') {
    const resumed = await api<PdfImportOut>('/pdf-imports/' + item.id + '/retry', 'POST', {})
    signal.throwIfAborted()
    const pdf = new File([file], file.name, { type: 'application/pdf' })
    await sendContent(resumed.upload, pdf, progress, signal)
  }
  signal.throwIfAborted()
  return api<PdfImportOut>('/pdf-imports/' + item.id + '/start', 'POST', {})
}

export function PdfImportForm({ notebookId, tagIds, onOpen }: { notebookId: string; tagIds: string[]; onOpen: (id: string) => void }) {
  const [file, setFile] = useState<File | null>(null), [item, setItem] = useState<PdfImportOut | null>(null)
  const [busy, setBusy] = useState(false), [progress, setProgress] = useState(0), [error, setError] = useState<unknown>(null)
  const requestKey = useRef(createUuid()), controller = useRef<AbortController | null>(null), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false; controller.current?.abort() } }, [])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!file || busy) return
    setBusy(true); setError(null); setProgress(0)
    controller.current = new AbortController()
    try {
      validPdf(file)
      const created = await api<PdfImportOut>('/pdf-imports', 'POST', {
        filename: file.name, size: file.size, request_key: requestKey.current,
        notebook_id: notebookId || null, tag_ids: tagIds,
      })
      if (!alive.current) return
      setItem(created)
      await transfer(created, file, percent => { if (alive.current) setProgress(percent) }, controller.current.signal)
      if (alive.current) onOpen(created.note_id)
    } catch (failure) { if (alive.current) setError(failure) }
    finally { if (alive.current) setBusy(false) }
  }
  return <section className="panel pdf-import-panel">
    <h3>导入 PDF 笔记</h3>
    <p className="muted">一本 PDF 创建一篇多页图片笔记，保留原件并按页序转换。单份最多 50 MiB、200 页，后台处理期间可以离开页面。</p>
    <form onSubmit={event => void submit(event)}>
      <label>选择 PDF 文件<input type="file" accept="application/pdf,.pdf" disabled={busy} onChange={event => {
        setFile(event.target.files?.[0] || null); setItem(null); setError(null); requestKey.current = createUuid()
      }}/></label>
      {busy ? <p role="status">上传 PDF：{progress}%</p> : null}
      <NoteError error={error}/>
      <div className="note-actions"><button className="primary" disabled={!file || busy}>{busy ? '正在上传…' : '导入并转换 PDF'}</button>
        {item && !busy ? <button type="button" className="text-button" onClick={() => onOpen(item.note_id)}>打开已创建的笔记继续</button> : null}</div>
    </form>
  </section>
}

export function PdfImportStatus({ noteId, owner, onBusyChange, onNoteChanged }: {
  noteId: string; owner: string; onBusyChange: (busy: boolean) => void; onNoteChanged: (note: Note) => void
}) {
  const callbacks = useRef({ onBusyChange, onNoteChanged }); callbacks.current = { onBusyChange, onNoteChanged }
  const [working, setWorking] = useState(false), [progress, setProgress] = useState(0), [error, setError] = useState<unknown>(null)
  const controller = useRef<AbortController | null>(null), alive = useRef(true)
  const query = useQuery({ queryKey: ['notes', owner, noteId, 'pdf-imports'], queryFn: () => api<PdfImportOut[]>('/notes/' + noteId + '/pdf-imports'),
    refetchInterval: state => state.state.data?.some(item => ['queued', 'processing'].includes(item.status)) ? 2000 : false })
  const item = query.data?.[0]
  const signature = item ? item.id + ':' + item.status + ':' + item.completed_pages : ''
  useEffect(() => { alive.current = true; return () => { alive.current = false; controller.current?.abort(); callbacks.current.onBusyChange(false) } }, [])
  useEffect(() => {
    let current = true
    const busy = working || query.isPending || !!item && ['uploading', 'expired', 'queued', 'processing', 'failed'].includes(item.status)
    if (busy) callbacks.current.onBusyChange(true)
    if (item) {
      void api<Note>('/notes/' + noteId).then(note => {
        if (current && alive.current) { callbacks.current.onNoteChanged(note); callbacks.current.onBusyChange(busy) }
      }).catch(failure => { if (current && alive.current) { setError(failure); callbacks.current.onBusyChange(busy) } })
    } else callbacks.current.onBusyChange(busy)
    return () => { current = false }
  }, [signature, query.isPending, working, noteId])
  async function act(action: () => Promise<unknown>) {
    if (working) return
    setWorking(true); setError(null)
    try { await action(); if (alive.current) await query.refetch() }
    catch (failure) { if (alive.current) setError(failure) }
    finally { if (alive.current) setWorking(false) }
  }
  async function resume(file: File) {
    if (!item) return
    controller.current = new AbortController()
    await act(() => transfer(item, file, percent => { if (alive.current) setProgress(percent) }, controller.current!.signal))
  }
  async function download() {
    if (!item) return
    await act(async () => {
      const ticket=authSession.ticket();controller.current=new AbortController()
      const blob=await readPrivateFile(item.upload.file.id,'original',controller.current.signal)
      if(!alive.current||!authSession.current(ticket,owner))throw new SessionChangedError()
      const url=URL.createObjectURL(blob),link=document.createElement('a')
      try{link.href=url;link.download=item.filename;link.click()}
      finally{setTimeout(()=>URL.revokeObjectURL(url),1000)}
    })
  }
  if (!item) return query.error ? <NoteError error={query.error}/> : null
  return <section className="pdf-import-panel" aria-label="PDF 导入进度">
    <div className="panel-heading"><h3>PDF 原件与转换进度</h3><span className="badge">{labels[item.status]}</span></div>
    <p>{item.filename}</p>
    <p role="status">已转换 {item.completed_pages} / {item.page_count ?? '待识别'} 页{working && ['uploading', 'expired'].includes(item.status) ? ' · 上传 ' + progress + '%' : ''}</p>
    {item.page_count ? <progress max={item.page_count} value={item.completed_pages} aria-label="PDF 页面转换进度"/> : null}
    {item.error_code ? <p role="status" className="error">{errors[item.error_code] || '转换未完成，请重试。已生成页面会保留。'}</p> : null}
    <NoteError error={error || query.error}/>
    {['uploading', 'expired'].includes(item.status) ? <label>重新选择原 PDF 继续上传<input type="file" accept="application/pdf,.pdf" disabled={working} onChange={event => {
      const file = event.target.files?.[0]; event.target.value = ''; if (file) void resume(file)
    }}/></label> : null}
    <div className="note-actions">
      {item.status === 'failed' ? <button className="secondary" disabled={working} onClick={() => void act(() => api('/pdf-imports/' + item.id + '/retry', 'POST', {}))}>重试未完成页面</button> : null}
      {item.upload.status === 'completed' ? <button className="text-button" disabled={working} onClick={() => void download()}>下载原始 PDF</button> : null}
      <button className="text-button" disabled={working || query.isFetching} onClick={() => void query.refetch()}>刷新转换进度</button>
    </div>
    {['queued', 'processing'].includes(item.status) ? <p className="muted">后台正在转换，已完成的页面可以先阅读。转换完成前暂不修改笔记信息或调整页序。</p> : null}
  </section>
}
