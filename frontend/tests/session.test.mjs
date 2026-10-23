import assert from 'node:assert/strict'
import {test} from 'node:test'
import {createSessionStore,sessionHeaders,shouldRestoreSession,createSessionState,SessionChangedError} from '../src/session.ts'

function storage(){const data=new Map();return {getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)}}
const alice={session_token:'a'.repeat(43),csrf_token:'c'.repeat(43),scope:'alice-tab'}
const bob={session_token:'b'.repeat(43),csrf_token:'d'.repeat(43),scope:'bob-tab'}

test('two tabs retain separate accounts across refresh, replacement and logout',()=>{
  const firstStorage=storage(),secondStorage=storage()
  const first=createSessionStore(firstStorage),second=createSessionStore(secondStorage)
  first.save(alice);assert.equal(second.read(),null)
  second.save(bob);assert.deepEqual(first.read(),alice)
  assert.deepEqual(createSessionStore(firstStorage).read(),alice,'reload retains the current tab session')
  first.clear();assert.deepEqual(second.read(),bob)
  first.save(bob);assert.deepEqual(second.read(),bob)
})

test('invalid storage is discarded; blocked storage fails without a shared fallback',()=>{
  const data=storage();data.setItem('ai-notes:tab-session:v1','{invalid')
  assert.equal(createSessionStore(data).read(),null)
  const blocked=createSessionStore({getItem(){throw new Error('blocked')},setItem(){throw new Error('blocked')},removeItem(){}})
  assert.throws(()=>blocked.read(),/blocked/)
  assert.throws(()=>blocked.save(alice),/blocked/)
})

test('request authorization and CSRF always come from the same tab session',()=>{
  assert.deepEqual(sessionHeaders(alice),{'Authorization':'Bearer '+alice.session_token,'X-CSRF-Token': alice.csrf_token})
  assert.notDeepEqual(sessionHeaders(alice),sessionHeaders(bob))
})

test('only reload may restore credentials; fresh, copied or history navigations start signed out',()=>{
  assert.equal(shouldRestoreSession('reload'),true)
  for(const type of ['navigate','back_forward',undefined])assert.equal(shouldRestoreSession(type),false)
})

test('generation barriers invalidate old requests including same-owner relogin',()=>{
  const state=createSessionState(),old=state.login('owner-a',state.ticket())
  const closing=state.beginLogout(old)
  assert.equal(state.current(old,'owner-a'),false)
  state.invalidate(closing)
  const next=state.login('owner-a',state.ticket())
  assert.equal(state.invalidate(closing),false)
  assert.equal(state.snapshot(),next)
  assert.throws(()=>state.verify('owner-a',old),SessionChangedError)
})

test('ending one document session does not change another independently authenticated tab',()=>{
  const first=createSessionState(),second=createSessionState()
  first.login('owner-a',first.ticket());const peer=second.login('owner-a',second.ticket())
  first.invalidate(first.beginLogout(first.ticket()))
  assert.equal(first.snapshot().phase,'anonymous');assert.equal(second.snapshot(),peer)
  assert.equal(second.current(peer,'owner-a'),true)
})
