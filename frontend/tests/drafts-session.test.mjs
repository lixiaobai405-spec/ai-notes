import assert from 'node:assert/strict'
import {test} from 'node:test'
import {authSession,SessionChangedError} from '../src/session.ts'
import {writeDraft,readDraft,backupDraft,clearUserDrafts,draftKey} from '../src/features/notes/drafts.ts'

function memory(){const values=new Map();return{get length(){return values.size},key:i=>[...values.keys()][i]??null,getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)}}
const content={title:'synthetic',notebook_id:null,tag_ids:[],body_json:{type:'doc',content:[]},plain_text:'private draft'}
const draft={baseRevision:1,content,savedAt:'2026-10-20'}
test('logout blocks synchronous draft and import-backup writes before editor unmount',()=>{
  globalThis.localStorage=memory();globalThis.sessionStorage=memory();authSession.invalidate(authSession.ticket(),{broadcast:false});authSession.login('owner-a',authSession.ticket())
  writeDraft('owner-a','note',draft);backupDraft('owner-a','note',content);assert.deepEqual(readDraft('owner-a','note'),draft)
  const old=authSession.ticket();authSession.beginLogout(old)
  assert.throws(()=>writeDraft('owner-a','note',draft),SessionChangedError);assert.throws(()=>backupDraft('owner-a','note',content),SessionChangedError);assert.equal(readDraft('owner-a','note'),null)
  authSession.invalidate(authSession.ticket(),{broadcast:false});authSession.login('owner-a',authSession.ticket());assert.equal(authSession.current(old,'owner-a'),false)
})
test('owner-scoped cleanup includes import backups while retaining another owner data',()=>{
  globalThis.localStorage=memory();globalThis.sessionStorage=memory();localStorage.setItem(draftKey('owner-a','note'),'draft');localStorage.setItem(draftKey('owner-a','note')+':before-import','backup');localStorage.setItem(draftKey('owner-b','note'),'other owner');sessionStorage.setItem('ai-notes:chat:owner-a','chat');sessionStorage.setItem('ai-notes:draft:v1:owner-a:section','notes');assert.equal(clearUserDrafts('owner-a'),true);assert.equal(localStorage.length,1);assert.equal(localStorage.getItem(draftKey('owner-b','note')),'other owner');assert.equal(sessionStorage.length,0)
})
test('inaccessible browser storage reports cleanup failure without preventing session lock',()=>{
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'localStorage');authSession.invalidate(authSession.ticket(),{broadcast:false});authSession.login('owner-a',authSession.ticket())
  try{Object.defineProperty(globalThis,'localStorage',{configurable:true,get(){throw new DOMException('blocked','SecurityError')}});const old=authSession.ticket();assert.doesNotThrow(()=>{authSession.invalidate(old);assert.equal(clearUserDrafts('owner-a'),false)});assert.equal(authSession.snapshot().phase,'anonymous');assert.equal(authSession.current(old,'owner-a'),false)}finally{Object.defineProperty(globalThis,'localStorage',descriptor)}
})
