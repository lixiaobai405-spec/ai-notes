import type { components } from './api.generated'
import { authSession,clearSession,initializeTabSession,readSession,saveSession,sessionHeaders,SessionChangedError,type TabSession } from './session.ts'
export type User = components['schemas']['UserOut']
export type Invite = components['schemas']['InviteOut']
export type CreatedInvite = components['schemas']['CreatedInvite']
export type Job = components['schemas']['JobOut']
export type Notice = components['schemas']['NotificationOut']
export type Audit = components['schemas']['AuditOut']
export type LoginInput = components['schemas']['LoginIn']
export type RegisterInput = components['schemas']['RegisterIn']
export type ResetInput = components['schemas']['ResetIn']
export type AiCatalog = components['schemas']['AiCatalogOut']
export type AiConnection = components['schemas']['AiConnectionOut']
export class ApiError extends Error {
  status:number;code:string;requestId:string
  constructor(status:number,code:string,message:string,requestId:string){super(message);this.status=status;this.code=code;this.requestId=requestId}
}
export async function authContext(){
  const before=authSession.ticket()
  await initializeTabSession()
  if(!authSession.current(before)&&authSession.snapshot().phase!=='anonymous')throw new SessionChangedError()
  const session=readSession(),ticket=authSession.ticket()
  if(!session){authSession.invalidate(ticket);throw new ApiError(401,'login_required','请先登录','')}
  const check=()=>{if(!authSession.current(ticket))throw new SessionChangedError()}
  check()
  return {session,ticket,headers:sessionHeaders(session),check}
}
export function rejectExpiredSession(response:Pick<Response,'status'>,session:TabSession|null){if(response.status===401&&session)clearSession(session.session_token)}
async function sendAuthorized(path:string,init:RequestInit,context:Awaited<ReturnType<typeof authContext>>){
  const controller=new AbortController(),external=init.signal
  const unsubscribe=authSession.subscribe(()=>{if(!authSession.current(context.ticket))controller.abort()})
  const signal=external?AbortSignal.any([external,controller.signal]):controller.signal
  const timeout=path==='/auth/logout'?setTimeout(()=>controller.abort(),12000):undefined
  try{
    context.check()
    if(context.ticket.phase==='closing'&&path!=='/auth/logout')throw new SessionChangedError()
    const headers=new Headers(init.headers)
    for(const [name,value] of Object.entries(context.headers))headers.set(name,value)
    const response=await fetch('/api/v1'+path,{...init,headers,signal,credentials:'omit',redirect:'error'})
    try{context.check()}catch(error){void response.body?.cancel().catch(()=>{});throw error}
    rejectExpiredSession(response,context.session);context.check()
    return response
  }catch(error){context.check();throw error}
  finally{unsubscribe();clearTimeout(timeout)}
}
export async function apiResponse(path:string,init:RequestInit={}):Promise<Response>{return sendAuthorized(path,init,await authContext())}
export async function logoutTab():Promise<string>{
  const context=await authContext()
  const response=await sendAuthorized('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'},context)
  if(!response.ok){const data=await response.json();context.check();throw new ApiError(response.status,data.code,data.message,data.request_id)}
  return context.session.session_token
}
export async function api<T>(path:string,method='GET',body?:unknown):Promise<T>{
  const publicAuth=['/auth/tab/login','/auth/tab/register','/auth/tab/password/reset'].includes(path)
  const headers:Record<string,string>=body===undefined?{}:{'Content-Type':'application/json'}
  let response:Response,check:()=>void
  if(publicAuth){
    await initializeTabSession()
    const ticket=authSession.ticket(),controller=new AbortController()
    check=()=>{if(!authSession.current(ticket)||ticket.phase==='closing')throw new SessionChangedError()}
    check()
    const probe='ai-notes:storage-probe';window.sessionStorage.setItem(probe,'1');window.sessionStorage.removeItem(probe)
    headers['X-Notes-Client']='tab'
    const unsubscribe=authSession.subscribe(()=>{if(!authSession.current(ticket))controller.abort()})
    try{response=await fetch('/api/v1'+path,{method,headers,credentials:'omit',redirect:'error',signal:controller.signal,body:body===undefined?undefined:JSON.stringify(body)});check()}
    catch(error){check();throw error}finally{unsubscribe()}
  }else{
    const context=await authContext();check=context.check
    response=await sendAuthorized(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)},context)
  }
  if(response.status===204)return undefined as T
  const data=await response.json();check()
  if(!response.ok)throw new ApiError(response.status,data.code,data.message,data.request_id)
  return data as T
}
export async function authenticate(path:'/auth/tab/login'|'/auth/tab/register',body:LoginInput|RegisterInput){
  await initializeTabSession()
  const ticket=authSession.ticket()
  const result=await api<components['schemas']['TabSessionOut']>(path,'POST',body)
  if(!authSession.current(ticket))throw new SessionChangedError()
  saveSession({session_token:result.session_token,csrf_token:result.csrf_token})
  authSession.verify(result.user.id,authSession.ticket())
  return {user:result.user,ticket:authSession.ticket()}
}
export const date=(s:string)=>new Date(s).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
