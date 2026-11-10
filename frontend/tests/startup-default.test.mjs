import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import {createRequire} from 'node:module'
import {createStartupDefaults} from '../src/features/personalization/startup-default.ts'
const require=createRequire(new URL('../package.json',import.meta.url)),ts=require('typescript')
function memoryStorage(){const data=new Map();return {data,getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)}}
test('startup applied marker survives document remount but is isolated by owner and persistent session scope',()=>{
 const storage=memoryStorage();let scope='login-one'
 const first=createStartupDefaults(()=>storage,()=>scope)
 assert.equal(first.has('alice'),false);first.mark('alice');assert.equal(first.has('alice'),true)
 const reload=createStartupDefaults(()=>storage,()=>scope);assert.equal(reload.has('alice'),true)
 assert.equal(reload.has('bob'),false);scope='login-two';assert.equal(reload.has('alice'),false)
 reload.mark('alice');assert.deepEqual([...storage.data.values()],['login-two'])
 reload.clear('alice');assert.equal(reload.has('alice'),false)
})
test('startup marker stores only the nonsecret scope, tolerates unavailable storage and missing session',()=>{
 const storage=memoryStorage(),missing=createStartupDefaults(()=>storage,()=> '')
 missing.mark('alice');assert.equal(storage.data.size,0)
 const blocked=createStartupDefaults(()=>{throw new Error('storage denied')},()=> 'scope')
 assert.equal(blocked.has('alice'),false);assert.doesNotThrow(()=>blocked.mark('alice'));assert.doesNotThrow(()=>blocked.clear('alice'))
})
const jsx=(type,props)=>({type,props}),noop=()=>{},text=node=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):''
const appSource=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8')
const appCode=ts.transpileModule(appSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText
/** Executes the real App startup effect; only browser, query and child rendering are replaced. */
function mount({storage,owner='alice',scope='login-one',id='document-one',initialPreferences={default_section:'cards'},restored=''}={}){
 let slots=[],index=0,effects=[],tree,preferences=initialPreferences,remembered=restored
 const session={id,generation:1,owner,phase:'active'},user={id:owner,display_name:'Synthetic',username:'synthetic',role:'user'}
 const react={useState(v){const n=index++;if(!(n in slots))slots[n]=typeof v==='function'?v():v;return [slots[n],value=>slots[n]=typeof value==='function'?value(slots[n]):value]},useRef(v){const n=index++;return slots[n]??(slots[n]={current:v})},useEffect(fn,deps){const n=index++;if(!slots[n]||deps.some((v,i)=>v!==slots[n][i])){slots[n]=deps;effects.push(fn)}},useSyncExternalStore:(_sub,snapshot)=>snapshot(),useCallback:f=>f,lazy:()=>()=>null,Suspense:'Suspense'}
 const query={useQueryClient:()=>({cancelQueries:async()=>{},clear:noop,invalidateQueries:noop,setQueryData:noop}),useQuery:()=>({data:user,isPending:false}),useMutation:()=>({isPending:false})}
 const fallback=new Proxy({},{get:()=>noop}),exports={}
 const requireMock=id=>id==='react'?react:id==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:id==='@tanstack/react-query'?query:id==='./session.ts'?{authSession:{snapshot:()=>session,subscribe:()=>noop,ticket:()=>session},readSession:()=>({scope}),startSessionSync:()=>noop}:id==='./features/personalization/startup-default'?{createStartupDefaults}:id==='./features/personalization/usePersonalization'?{usePersonalization:()=>({data:preferences})}:id==='./features/personalization/personalization'?{homeSection:v=>v,shouldApplyHomeDefault:v=>!Object.values(v).some(Boolean)}:id==='./features/notes/drafts'?{wasNotesOpen:()=>remembered==='notes',wasChatOpen:()=>remembered==='chat',rememberSection:(_owner,value)=>remembered=value}:id==='./api'?{ApiError:class extends Error{}}:fallback
 vm.runInNewContext(appCode,{exports,require:requireMock,console,window:{sessionStorage:storage}},{filename:'App.formal.js'})
 const render=()=>{index=0;tree=exports.default();const list=effects;effects=[];list.forEach(f=>f());return tree}
 function elements(node,out=[]){if(Array.isArray(node))node.forEach(n=>elements(n,out));else if(node&&typeof node==='object'){out.push(node);elements(node.props?.children,out)}return out}
 return {render,route:()=>text(elements(tree).find(n=>n.type==='h1')),click:label=>elements(tree).find(n=>n.type==='button'&&text(n)===label).props.onClick(),preferences:value=>preferences=value,remembered:()=>remembered}
}
test('actual App applies fresh login default once, then reload with a new document ID does not jump to default again',()=>{
 const storage=memoryStorage(),first=mount({storage});first.render();first.render();assert.match(first.route(),/学习卡片/)
 first.click('学习社区');first.render();assert.match(first.route(),/学习社区/)
 const reload=mount({storage,id:'document-two'});reload.render();reload.render();assert.doesNotMatch(reload.route(),/学习卡片/)
 const newLogin=mount({storage,id:'document-three',scope:'login-two'});newLogin.render();newLogin.render();assert.match(newLogin.route(),/学习卡片/)
 const otherOwner=mount({storage,owner:'bob'});otherOwner.render();otherOwner.render();assert.match(otherOwner.route(),/学习卡片/)
})
test('actual App late preferences never override manual navigation, including after reload before preferences arrive',()=>{
 const storage=memoryStorage(),first=mount({storage,initialPreferences:null});first.render();first.render();assert.doesNotMatch(first.route(),/学习卡片/)
 first.click('学习社区');first.render();first.preferences({default_section:'cards'});first.render();first.render();assert.match(first.route(),/学习社区/)
 const reload=mount({storage,id:'new-document'});reload.render();reload.render();assert.doesNotMatch(reload.route(),/学习卡片/)
})
test('actual App keeps restored notes/chat precedence both before and after marker persistence',()=>{
 for(const restored of ['notes','chat']){const storage=memoryStorage();for(const id of ['first','reload']){const app=mount({storage,id,restored});app.render();app.render();assert.match(app.route(),restored==='notes'?/我的笔记/:/AI 对话/)}}
})

test('actual App opens notes while homepage preferences are unavailable',()=>{
 const app=mount({storage:memoryStorage(),initialPreferences:null});app.render();app.render()
 assert.match(app.route(),/我的笔记/)
})

test('actual App settings do not replace the last notes or chat restoration point',()=>{
 for(const restored of ['notes','chat']){
  const storage=memoryStorage(),app=mount({storage,restored});app.render();app.render()
  app.click('设置');app.render();assert.match(app.route(),/设置/)
  assert.equal(app.remembered(),restored)
  const reload=mount({storage,id:'after-settings',restored:app.remembered()});reload.render();reload.render()
  assert.match(reload.route(),restored==='notes'?/我的笔记/:/AI 对话/)
 }
})
