import { api,apiResponse } from '../../api.ts'
import { authSession,SessionChangedError } from '../../session.ts'
import type { TicketOut } from './types'

export async function readPrivateFile(fileId:string,kind:'original'|'reading'|'thumbnail',signal:AbortSignal):Promise<Blob>{
  const ticket=authSession.ticket(),controller=new AbortController()
  const check=()=>{signal.throwIfAborted();if(!authSession.current(ticket))throw new SessionChangedError()}
  const unsubscribe=authSession.subscribe(()=>{if(!authSession.current(ticket))controller.abort()})
  try{
    check()
    const result=await api<TicketOut>('/files/'+fileId+'/ticket?kind='+kind,'POST',{})
    check()
    const target=new URL(result.url,window.location.origin)
    if(target.origin!==window.location.origin||target.pathname!==`/api/v1/files/${fileId}/content`)throw new Error('文件地址不符合私有访问规则')
    const response=await apiResponse(target.pathname.slice('/api/v1'.length)+target.search,{signal:AbortSignal.any([signal,controller.signal])})
    check()
    if(!response.ok)throw new Error('文件读取失败或授权已过期，请重新加载')
    const blob=await response.blob();check()
    return blob
  }catch(error){check();throw error}
  finally{unsubscribe();controller.abort()}
}
