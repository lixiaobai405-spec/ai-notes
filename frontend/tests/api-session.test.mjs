import assert from 'node:assert/strict'
import {test} from 'node:test'
import {api,ApiError,authenticate} from '../src/api.ts'
import {authSession,clearSession,SessionChangedError,readSession} from '../src/session.ts'
import {signIn,tick} from './tab-environment.mjs'

const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return{promise,resolve}}
const response=(status,json)=>({status,ok:status<400,json:async()=>json})

test('mutations send one immutable tab credential pair without a cookie CSRF request',async()=>{
  const original=globalThis.fetch,calls=[];signIn('owner-a','a')
  try{globalThis.fetch=async(url,options)=>{calls.push({url,options});return response(200,{ok:true})};await api('/notes','POST',{title:'private'});assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/v1/notes');assert.equal(calls[0].options.credentials,'omit');assert.equal(calls[0].options.redirect,'error');assert.equal(calls[0].options.headers.get('Authorization'),'Bearer '+'a'.repeat(43));assert.equal(calls[0].options.headers.get('X-CSRF-Token'),readSession().csrf_token)}finally{globalThis.fetch=original}
})
test('a mutation cannot take replacement credentials while awaiting initialization',async()=>{
  const original=globalThis.fetch,calls=[];signIn('owner-a','a')
  try{globalThis.fetch=async(...args)=>{calls.push(args);return response(200,{})};const work=api('/notes','POST',{title:'owner-a draft'});const rejected=assert.rejects(work,SessionChangedError);signIn('owner-b','b');await rejected;assert.equal(calls.length,0)}finally{globalThis.fetch=original}
})
test('late JSON content from an old session is discarded even if transport ignores abort',async()=>{
  const original=globalThis.fetch,body=deferred();signIn('owner-a','a')
  try{globalThis.fetch=async()=>({status:200,ok:true,json:()=>body.promise});const work=api('/notes/private');const rejected=assert.rejects(work,SessionChangedError);await tick();signIn('owner-b','b');body.resolve({plain_text:'old private body'});await rejected;assert.equal(authSession.snapshot().owner,'owner-b')}finally{globalThis.fetch=original}
})
test('old response headers cannot invalidate a new account session',async()=>{
  const original=globalThis.fetch,headers=deferred();signIn('owner-a','a')
  try{globalThis.fetch=()=>headers.promise;const work=api('/auth/me');const rejected=assert.rejects(work,SessionChangedError);await tick();const next=signIn('owner-b','b');headers.resolve(response(401,{code:'unauthenticated'}));await rejected;assert.equal(authSession.snapshot(),next)}finally{globalThis.fetch=original}
})
test('invalid public credentials keep the anonymous generation and omit cookies',async()=>{
  const original=globalThis.fetch;clearSession();const before=authSession.ticket()
  try{globalThis.fetch=async(url,options)=>{assert.equal(url,'/api/v1/auth/tab/login');assert.equal(options.credentials,'omit');assert.equal(options.headers['X-Notes-Client'],'tab');return response(401,{code:'credentials_invalid',message:'invalid',request_id:'test'})};await assert.rejects(api('/auth/tab/login','POST',{username:'synthetic',password:'synthetic'}),error=>error instanceof ApiError&&error.status===401);assert.equal(authSession.snapshot(),before)}finally{globalThis.fetch=original}
})
test('a successful login stores tab credentials and immediately verifies its owner',async()=>{
  const original=globalThis.fetch;clearSession()
  try{globalThis.fetch=async()=>response(200,{session_token:'x'.repeat(43),csrf_token:'y'.repeat(43),user:{id:'owner-x'}});const result=await authenticate('/auth/tab/login',{username:'synthetic',password:'synthetic'});assert.equal(result.user.id,'owner-x');assert.equal(readSession().session_token,'x'.repeat(43));assert.equal(authSession.current(result.ticket,'owner-x'),true)}finally{globalThis.fetch=original}
})
test('simultaneous login JSON results cannot overwrite the credentials committed by the first continuation',async()=>{
  const original=globalThis.fetch;clearSession();const bodies=[]
  globalThis.fetch=async()=>({status:200,ok:true,json:()=>new Promise(resolve=>bodies.push(resolve))})
  try{
    const a=authenticate('/auth/tab/login',{username:'alice',password:'synthetic'}),b=authenticate('/auth/tab/login',{username:'bob',password:'synthetic'})
    const outcomes=Promise.allSettled([a,b]);await tick();assert.equal(bodies.length,2)
    bodies[1]({session_token:'b'.repeat(43),csrf_token:'d'.repeat(43),user:{id:'bob'}})
    bodies[0]({session_token:'a'.repeat(43),csrf_token:'c'.repeat(43),user:{id:'alice'}})
    const result=await outcomes
    assert.equal(result[1].status,'fulfilled');assert.equal(result[0].status,'rejected');assert.ok(result[0].reason instanceof SessionChangedError)
    assert.equal(readSession().session_token,'b'.repeat(43));assert.equal(authSession.snapshot().owner,'bob')
  }finally{globalThis.fetch=original}
})
test('a current private 401 removes credentials before callers can reuse its content',async()=>{
  const original=globalThis.fetch;signIn('owner-a','a')
  try{globalThis.fetch=async()=>response(401,{code:'unauthenticated',message:'login required'});await assert.rejects(api('/notes/private'),SessionChangedError);assert.equal(authSession.snapshot().phase,'anonymous');assert.equal(readSession(),null)}finally{globalThis.fetch=original}
})
test('logout is allowed inside its closing barrier while other mutations are blocked',async()=>{
  const original=globalThis.fetch,calls=[];signIn('owner-a','a');const closing=authSession.beginLogout(authSession.ticket())
  try{globalThis.fetch=async url=>{calls.push(url);return response(204,{})};await assert.rejects(api('/notes','POST',{}),SessionChangedError);await api('/auth/logout','POST',{});assert.deepEqual(calls,['/api/v1/auth/logout']);assert.equal(authSession.current(closing),true);clearSession();assert.equal(authSession.snapshot().phase,'anonymous')}finally{globalThis.fetch=original}
})
