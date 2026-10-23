import {authSession,initializeTabSession,saveSession} from '../src/session.ts'
export function memory(){const data=new Map();return{getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)}}
const events=new EventTarget()
export const storage=memory()
globalThis.window={sessionStorage:storage,location:{origin:'http://testserver'},dispatchEvent:events.dispatchEvent.bind(events),addEventListener:events.addEventListener.bind(events),removeEventListener:events.removeEventListener.bind(events)}
globalThis.sessionStorage=storage
globalThis.BroadcastChannel=undefined
Object.defineProperty(globalThis,'performance',{value:{timeOrigin:100,getEntriesByType:()=>[{type:'reload'}]},configurable:true})
await initializeTabSession()
export function signIn(owner='owner-a',token=owner[0]||'a'){
  saveSession({session_token:token.repeat(43),csrf_token:('csrf-'+token).repeat(8)})
  return authSession.verify(owner,authSession.ticket())
}
export const tick=()=>new Promise(resolve=>setImmediate(resolve))