import { createUuid } from './uuid.ts'

export type SessionPhase='checking'|'active'|'anonymous'|'closing'
export type SessionTicket={generation:number;id:string;owner:string|null;phase:SessionPhase;until?:number}
export class SessionChangedError extends Error {
  constructor(){super('登录状态已变化，请重新登录后操作。');this.name='AbortError'}
}

// Generations protect this document; independent tabs never share logout.
export function createSessionState() {
  let state:SessionTicket={generation:0,id:createUuid(),owner:null,phase:'checking'}
  const listeners=new Set<()=>void>()
  const snapshot=()=>state
  const current=(ticket:SessionTicket,owner?:string)=>ticket.generation===state.generation&&ticket.id===state.id&&(owner===undefined||state.phase==='active'&&state.owner===owner)
  function update(phase:SessionPhase,owner:string|null,advance=true,until?:number){state={generation:state.generation+(advance?1:0),id:advance?createUuid():state.id,owner,phase,until};listeners.forEach(listener=>listener());return state}
  return {
    snapshot,ticket:snapshot,current,
    subscribe(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}},
    login(owner:string,ticket:SessionTicket){if(!current(ticket)||state.phase==='closing')throw new SessionChangedError();return update('active',owner)},
    verify(owner:string,ticket:SessionTicket){if(!current(ticket)||!['checking','active'].includes(state.phase))throw new SessionChangedError();return update('active',owner,state.owner!==null&&state.owner!==owner)},
    beginLogout(ticket:SessionTicket,until=Date.now()+15000){if(!current(ticket)||state.phase!=='active')throw new SessionChangedError();return update('closing',state.owner,true,until)},
    invalidate(ticket:SessionTicket,_options?:{broadcast?:boolean}){if(!current(ticket)||state.phase==='anonymous')return false;update('anonymous',null);return true},
    reset(){return update('checking',null)},
    check(){if(state.phase==='active')update('checking',state.owner)},
  }
}

const storageKey='ai-notes:tab-session:v1'
export const sessionEndedEvent='ai-notes:session-ended'
export interface TabSession { session_token:string;csrf_token:string;scope:string }
export const authSession=createSessionState()

export function createSessionStore(storage:Pick<Storage,'getItem'|'setItem'|'removeItem'>){
  return {
    read():TabSession|null {
      const raw=storage.getItem(storageKey)
      if(!raw)return null
      try{const value=JSON.parse(raw);if(value&&typeof value.session_token==='string'&&value.session_token.length>=20&&typeof value.csrf_token==='string'&&value.csrf_token.length>=20&&typeof value.scope==='string')return value}catch{/* Discard malformed credentials without logging them. */}
      storage.removeItem(storageKey);return null
    },
    save(value:TabSession){storage.setItem(storageKey,JSON.stringify(value))},
    clear(){storage.removeItem(storageKey)},
  }
}
function store(){return createSessionStore(window.sessionStorage)}
export function readSession(){return store().read()}
export function saveSession(value:Omit<TabSession,'scope'>){store().save({...value,scope:createUuid()});authSession.reset()}
export function clearSession(expectedToken?:string){
  if(expectedToken&&readSession()?.session_token!==expectedToken)return
  store().clear();authSession.invalidate(authSession.ticket())
  window.dispatchEvent(new Event(sessionEndedEvent))
}
export function sessionHeaders(session:TabSession):Record<string,string>{return {'Authorization':'Bearer '+session.session_token,'X-CSRF-Token':session.csrf_token}}

let initialization:Promise<void>|undefined
export function shouldRestoreSession(navigationType:string|undefined){return navigationType==='reload'}
export function initializeTabSession():Promise<void>{
  if(initialization)return initialization
  initialization=new Promise<void>(resolve=>{
    const navigation=performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming|undefined
    if(!shouldRestoreSession(navigation?.type))clearSession()
    if(typeof BroadcastChannel==='undefined'){resolve();return}
    // Only the random scope is broadcast to detect copied sessionStorage.
    const channel=new BroadcastChannel('ai-notes:tab-ownership:v1')
    const claim=createUuid(),started=performance.timeOrigin
    let active=false
    channel.onmessage=({data})=>{
      const current=readSession()
      if(!current||!data||data.scope!==current.scope)return
      const earlier=typeof data.started==='number'&&(data.started<started||(data.started===started&&data.claim<claim))
      if(data.type==='claim'&&data.claim!==claim&&(active||!earlier))channel.postMessage({type:'claimed',scope:current.scope,target:data.claim,claim,started})
      else if(data.type==='claimed'&&data.target===claim&&earlier)clearSession(current.session_token)
    }
    const current=readSession()
    if(!current){active=true;resolve();return}
    channel.postMessage({type:'claim',scope:current.scope,claim,started})
    setTimeout(()=>{active=true;resolve()},150)
  })
  return initialization
}

export function startSessionSync(onVerify?:()=>void){
  const focus=()=>{if(navigator.onLine!==false&&['active','checking'].includes(authSession.snapshot().phase))onVerify?.()}
  const visible=()=>{if(document.visibilityState==='visible')focus()}
  let timer:ReturnType<typeof setTimeout>|undefined
  const closing=()=>{clearTimeout(timer);const ticket=authSession.ticket();if(ticket.phase==='closing')timer=setTimeout(()=>{if(authSession.current(ticket))clearSession()},Math.max(0,(ticket.until||Date.now())-Date.now()))}
  const unsubscribe=authSession.subscribe(closing);closing()
  window.addEventListener('focus',focus);window.addEventListener('online',focus);document.addEventListener('visibilitychange',visible)
  return()=>{unsubscribe();clearTimeout(timer);window.removeEventListener('focus',focus);window.removeEventListener('online',focus);document.removeEventListener('visibilitychange',visible)}
}