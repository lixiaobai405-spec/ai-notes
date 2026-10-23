import assert from 'node:assert/strict'
import {test} from 'node:test'
import {apiResponse,logoutTab} from '../src/api.ts'
import {authSession,clearSession,readSession,saveSession,sessionEndedEvent,SessionChangedError} from '../src/session.ts'
import {signIn,tick,storage} from './tab-environment.mjs'

test('a late logout cannot clear a replacement login or return its success',async()=>{
  signIn('alice','a');let finish
  globalThis.fetch=async()=>new Promise(resolve=>{finish=resolve})
  const logout=logoutTab(),rejected=assert.rejects(logout,SessionChangedError);await tick()
  signIn('bob','b');finish(new Response('{"ok":true}'))
  await rejected;assert.equal(readSession().session_token,'b'.repeat(43))
})
test('the expected-token guard does not revoke a replacement credential pair',()=>{
  signIn('alice','a');signIn('bob','b');clearSession('a'.repeat(43))
  assert.equal(readSession().session_token,'b'.repeat(43))
})
test('a stale private response is discarded even when the mock transport ignores abort',async()=>{
  signIn('alice','a');let finish,sent
  globalThis.fetch=async(_url,init)=>{sent=init.headers.get('Authorization');return new Promise(resolve=>{finish=resolve})}
  const request=apiResponse('/auth/me'),rejected=assert.rejects(request,SessionChangedError);await tick()
  signIn('bob','b');finish(new Response('{}',{status:401}));await rejected
  assert.equal(sent,'Bearer '+'a'.repeat(43));assert.equal(readSession().session_token,'b'.repeat(43))
})
test('a delayed ownership reply still clears a copied reload session after the initial handshake',async()=>{
  saveSession({session_token:'a'.repeat(43),csrf_token:'c'.repeat(43)})
  let ended;const end=new Promise(resolve=>{ended=resolve})
  window.addEventListener(sessionEndedEvent,ended,{once:true})
  globalThis.BroadcastChannel=class{
    postMessage(message){if(message.type==='claim')setTimeout(()=>this.onmessage({data:{type:'claimed',scope:message.scope,target:message.claim,started:0,claim:'older-document'}}),300)}
  }
  const copy=await import('../src/session.ts?delayed-copy')
  await copy.initializeTabSession();assert.ok(copy.readSession());await end
  assert.equal(storage.getItem('ai-notes:tab-session:v1'),null)
  globalThis.BroadcastChannel=undefined
})