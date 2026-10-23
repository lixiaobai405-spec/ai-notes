import type { Content } from './types'
import { authSession,SessionChangedError } from '../../session.ts'
const prefix='ai-notes:draft:v1:'
export interface Draft { baseRevision:number; content:Content; savedAt:string }
export const draftKey=(owner:string,note:string)=>prefix+owner+':'+note
export function readDraft(owner:string,note:string):Draft|null {
  if(!authSession.current(authSession.ticket(),owner))return null
  try { const raw=localStorage.getItem(draftKey(owner,note)); if(!raw)return null; const d=JSON.parse(raw);return d&&Number.isInteger(d.baseRevision)&&d.baseRevision>0&&typeof d.content?.title==='string'&&Array.isArray(d.content?.tag_ids)&&d.content.tag_ids.every((id:unknown)=>typeof id==='string')&&(d.content.notebook_id===null||typeof d.content.notebook_id==='string')&&d.content.body_json&&typeof d.content.body_json==='object'&&!Array.isArray(d.content.body_json)&&typeof d.content?.plain_text==='string'?d:null }catch{return null}
}
export function writeDraft(owner:string,note:string,draft:Draft) { if(!authSession.current(authSession.ticket(),owner))throw new SessionChangedError();localStorage.setItem(draftKey(owner,note),JSON.stringify(draft)) }
export function clearDraft(owner:string,note:string){localStorage.removeItem(draftKey(owner,note))}
export function backupDraft(owner:string,note:string,content:Content){if(!authSession.current(authSession.ticket(),owner))throw new SessionChangedError();localStorage.setItem(draftKey(owner,note)+':before-import',JSON.stringify(content))}
export function clearUserDrafts(owner:string) {
  let cleared=true
  try{const keys:string[]=[];for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);if(k?.startsWith(prefix+owner+':'))keys.push(k)}for(const k of keys){try{localStorage.removeItem(k)}catch{cleared=false}}}catch{cleared=false}
  for(const k of [prefix+owner+':selected-note',prefix+owner+':section','ai-notes:chat:'+owner]){try{sessionStorage.removeItem(k)}catch{cleared=false}}
  return cleared
}
export function selectedNote(owner:string):string {try{return sessionStorage.getItem(prefix+owner+':selected-note')||''}catch{return ''}}
export function rememberNote(owner:string,id:string){try{if(id)sessionStorage.setItem(prefix+owner+':selected-note',id);else sessionStorage.removeItem(prefix+owner+':selected-note')}catch{/* Navigation still works when browser storage is unavailable. */}}
export function wasNotesOpen(owner:string):boolean{try{return sessionStorage.getItem(prefix+owner+':section')==='notes'}catch{return false}}
export function rememberSection(owner:string,section:string){try{sessionStorage.setItem(prefix+owner+':section',section)}catch{/* Navigation still works when browser storage is unavailable. */}}

export function wasChatOpen(owner:string):boolean{try{return sessionStorage.getItem(prefix+owner+':section')==='chat'}catch{return false}}
export function clearTabNavigation(owner:string){let cleared=true;for(const key of [prefix+owner+':selected-note',prefix+owner+':section','ai-notes:chat:'+owner]){try{sessionStorage.removeItem(key)}catch{cleared=false}}return cleared}
