import { ApiError,authContext,rejectExpiredSession } from '../../api.ts'
import { authSession,SessionChangedError } from '../../session.ts'
import type { UploadOut } from './types'

export async function sendContent(upload:UploadOut,file:File,progress:(percent:number)=>void,signal:AbortSignal):Promise<UploadOut>{
  const ticket=authSession.ticket()
  const check=()=>{signal.throwIfAborted();if(!authSession.current(ticket))throw new SessionChangedError()}
  check()
  const target=new URL(upload.upload_url,window.location.origin)
  if(target.origin!==window.location.origin||target.pathname!==`/api/v1/uploads/${upload.id}/content`||upload.upload_method!=='PUT')throw new Error('上传地址不符合私有访问规则')
  const context=await authContext();check()
  return new Promise((resolve,reject)=>{
    const request=new XMLHttpRequest()
    let settled=false,unsubscribe=()=>{}
    function finish(error:unknown,value?:UploadOut){if(settled)return;settled=true;signal.removeEventListener('abort',abort);unsubscribe();if(error)reject(error);else resolve(value!)}
    const abort=()=>{request.abort();finish(new DOMException('已停止上传','AbortError'))}
    request.open('PUT',target.pathname+target.search)
    request.withCredentials=false
    request.setRequestHeader('Content-Type',file.type)
    for(const [name,value] of Object.entries(context.headers))request.setRequestHeader(name,value)
    request.upload.onprogress=e=>{if(authSession.current(ticket)&&e.lengthComputable)progress(Math.round(e.loaded/e.total*100))}
    request.onload=()=>{try{check();const data=JSON.parse(request.responseText);rejectExpiredSession(request,context.session);check();if(request.status>=200&&request.status<300)finish(null,data as UploadOut);else finish(new ApiError(request.status,data.code||'upload_failed',data.message||'文件上传失败',data.request_id||''))}catch(error){finish(error instanceof SessionChangedError||signal.aborted?error:new Error('上传服务返回无效响应，请重试'))}}
    request.onerror=()=>finish(new Error('网络中断，文件尚未上传完成，请重试'))
    request.onabort=()=>finish(new DOMException('已停止上传','AbortError'))
    unsubscribe=authSession.subscribe(()=>{if(!authSession.current(ticket)){finish(new SessionChangedError());request.abort()}})
    signal.addEventListener('abort',abort,{once:true})
    try{check();request.send(file)}catch(error){finish(error)}
  })
}