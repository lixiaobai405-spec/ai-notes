import type { components } from '../../api.generated'
import { authSession, SessionChangedError } from '../../session.ts'
import { apiResponse } from '../../api.ts'

export type Run = components['schemas']['RunOut']
export type Conversation = components['schemas']['ConversationOut']
export type Detail = components['schemas']['ConversationDetail']
export const active = (run?:Run) => run?.status === 'queued' || run?.status === 'running'

// Fetch only reads an existing run: reconnecting cannot submit another paid request.
export async function readStream(id:string, signal:AbortSignal, receive:(name:string, data:unknown)=>void) {
  const ticket=authSession.ticket()
  const check=()=>{signal.throwIfAborted();if(!authSession.current(ticket))throw new SessionChangedError()}
  check()
  const response = await apiResponse(`/ai/runs/${id}/events`, {signal})
  try{check()}catch(error){await response.body?.cancel().catch(()=>{});throw error}
  if (!response.ok) {
    const error = await response.json()
    check()
    if(response.status===401&&ticket.owner)authSession.invalidate(ticket)
    throw new Error(error.message || '无法读取回复，请刷新后重试')
  }
  if (!response.body) throw new Error('无法读取回复，请刷新查看已保存的结果')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', done = false
  try {
    while (true) {
      const chunk = await reader.read()
      check()
      buffer = (buffer+decoder.decode(chunk.value,{stream:!chunk.done})).replace(/\r\n/g,'\n')
      let boundary:number
      while ((boundary=buffer.indexOf('\n\n')) !== -1) {
        if(boundary>1_000_000)throw new Error('回复格式异常，请重新读取已保存的结果')
        const block=buffer.slice(0,boundary)
        buffer=buffer.slice(boundary+2)
        const lines=block.split('\n')
        const name=lines.find(line=>line.startsWith('event:'))?.slice(6).trim()
        const data=lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n')
        if (name && data) {
          check()
          const parsed:unknown=JSON.parse(data)
          if (name==='error') throw new Error((parsed as {message:string}).message)
          receive(name,parsed)
          if (name==='done') done=true
        }
      }
      if(buffer.length>1_000_000)throw new Error('回复格式异常，请重新读取已保存的结果')
      if (chunk.done) break
    }
    if (!done) throw new Error('连接已断开，已保存的内容仍在。点击“重新读取”查看原回复。')
  } finally {
    await reader.cancel().catch(()=>{})
    reader.releaseLock()
  }
}
