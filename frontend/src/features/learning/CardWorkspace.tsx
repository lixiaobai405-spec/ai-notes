import {useCallback,useEffect,useId,useRef,useState,useSyncExternalStore} from 'react'
import {useQuery,useQueryClient} from '@tanstack/react-query'
import {Folders,SlidersHorizontal} from 'lucide-react'
import {api,ApiError} from '../../api'
import {authSession} from '../../session'
import {NoteError} from '../notes/NoteError'
import {CardEditor} from './CardEditor'
import {CardGenerations} from './CardGenerations'
import {CardPreview,CardSource} from './CardPreview'
import {kindNames,sourceVisual,statusNames,selectionOnPage,updatePageSelection,allPageSelected,type PageSelection,type Card,type CardList,type Deck,type DeckList,type Kind,type SourceRead} from './cards'
import './cards.css'
import {useLearningActivity} from './ActivityTracker'

const pageSize=50
type Props={owner:string;onOpenNote:(id:string)=>void}
export default function CardWorkspace(props:Props){
  const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot)
  if(session.phase!=='active'||session.owner!==props.owner)return null
  return <Workspace key={props.owner+':'+session.id} {...props} sessionId={session.id}/>
}
function Workspace({owner,sessionId,onOpenNote}:Props&{sessionId:string}){
  const client=useQueryClient(),[ticket]=useState(()=>authSession.ticket()),[deckOffset,setDeckOffset]=useState(0),[newDeck,setNewDeck]=useState(''),[deckFilter,setDeckFilter]=useState(''),[status,setStatus]=useState<Card['status']|''>(''),[kind,setKind]=useState<Kind|''>(''),[offset,setOffset]=useState(0)
  const [selection,setSelection]=useState<PageSelection<Card>>({pageIds:[],items:[]}),[editor,setEditor]=useState<Card|'new'|null>(null),[reading,setReading]=useState(''),[showGeneration,setShowGeneration]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null),[notice,setNotice]=useState('')
  const [panel,setPanel]=useState<'filters'|'decks'|null>(null)
  const panelId=useId(),newDeckInput=useRef<HTMLInputElement>(null),selectPageInput=useRef<HTMLInputElement>(null)
  const lock=useRef(false),active=()=>authSession.current(ticket,owner)&&ticket.id===sessionId
  const decks=useQuery({queryKey:['card-decks',owner,sessionId,deckOffset],queryFn:()=>api<DeckList>('/learning/decks?limit=50&offset='+deckOffset),retry:false})
  const params=new URLSearchParams({limit:String(pageSize),offset:String(offset)});if(deckFilter)params.set('deck_id',deckFilter);if(status)params.set('status',status);if(kind)params.set('kind',kind)
  const cards=useQuery({queryKey:['cards',owner,sessionId,params.toString()],queryFn:()=>api<CardList>('/learning/cards?'+params),retry:false})
  const currentPage=cards.data?.items||[],pageMembership=currentPage.map(card=>card.id).join(';')
  const selected=selectionOnPage(selection,currentPage).items
  const allSelected=allPageSelected(selected,currentPage)
  const activeFilters=[status&&statusNames[status],kind&&kindNames[kind]].filter(Boolean)
  // Derive the scoped selection immediately; the effect then discards obsolete snapshots.
  useEffect(()=>setSelection(old=>selectionOnPage(old,currentPage)),[pageMembership])
  useEffect(()=>{if(panel==='decks')newDeckInput.current?.focus()},[panel])
  useEffect(()=>{if(selectPageInput.current)selectPageInput.current.indeterminate=selected.length>0&&!allSelected},[allSelected,selected.length])
  function setSelected(update:Card[]|((old:Card[])=>Card[])){setSelection(old=>updatePageSelection(old,currentPage,update))}
  const refresh=useCallback(()=>{void client.invalidateQueries({queryKey:['cards',owner,sessionId]})},[client,owner,sessionId])
  async function mutate(action:()=>Promise<unknown>,message:string){if(lock.current)return;lock.current=true;setBusy(true);setError(null);setNotice('');try{await action();if(active()){setNotice(message);setSelected([]);void client.invalidateQueries({queryKey:['card-decks',owner,sessionId]});refresh()}}catch(e){if(active())setError(e)}finally{lock.current=false;if(active())setBusy(false)}}
  function filters(change:()=>void){change();setOffset(0);setSelected([]);setReading('');setError(null)}
  function page(next:number){setOffset(next);setSelected([]);setReading('')}
  function review(action:'confirm'|'archive'){const snapshot=selected.map(card=>({id:card.id,revision:card.revision}));void mutate(()=>api('/learning/cards/batch-review','POST',{action,cards:snapshot}),action==='confirm'?'整批卡片已确认，各自已建立独立复习计划，可在复习中心调整。':'整批卡片已归档，历史内容保留。')}
  if(!active())return null
  return <div className="card-workspace"><section className="panel card-library" aria-label="卡片列表">
    <div className="panel-heading card-library-heading">
      <h3>卡片列表</h3>
      <div className="card-actions">
        <button type="button" className="secondary" onClick={()=>{setEditor(null);setShowGeneration(value=>!value)}}>{showGeneration?'收起 AI 草稿':'AI 生成草稿'}</button>
        <button type="button" className="primary" disabled={!decks.data?.items.some(deck=>deck.status==='active')} onClick={()=>{setShowGeneration(false);setEditor('new');setReading('')}}>手工新建卡片</button>
      </div>
    </div>
    <div className="card-list-toolbar">
      <label className="card-deck-filter"><span>卡片集</span><select aria-label="筛选卡片集" value={deckFilter} onChange={e=>filters(()=>setDeckFilter(e.target.value))}>
        <option value="">全部卡片集</option>{decks.data?.items.map(deck=><option key={deck.id} value={deck.id}>{deck.name}{deck.status==='archived'?'（已归档）':''}</option>)}
      </select></label>
      <button type="button" className="secondary card-toolbar-button" aria-expanded={panel==='filters'} aria-controls={panelId+'-filters'} onClick={()=>setPanel(value=>value==='filters'?null:'filters')}><SlidersHorizontal size={15} aria-hidden="true"/>筛选</button>
      <button type="button" className="secondary card-toolbar-button" aria-expanded={panel==='decks'} aria-controls={panelId+'-decks'} onClick={()=>setPanel(value=>value==='decks'?null:'decks')}><Folders size={15} aria-hidden="true"/>管理卡片集</button>
      <button type="button" className="text-button card-list-refresh" onClick={()=>{setSelected([]);setReading('');setError(null);void cards.refetch()}}>刷新卡片</button>
    </div>
    {activeFilters.length?<p className="card-active-filters"><span>已筛选</span>{activeFilters.map(label=><span className="badge" key={label}>{label}</span>)}</p>:null}
    <NoteError error={error||cards.error||decks.error}/>
    {error instanceof ApiError&&error.status===409?<div className="card-generation-warning" role="alert">版本冲突，整批未写入。请刷新卡片并重新选择审核；当前编辑内容未被覆盖。<button type="button" className="text-button" onClick={()=>{setSelected([]);setError(null);refresh()}}>刷新冲突卡片</button></div>:null}
    {notice?<p className="muted" role="status">{notice}</p>:null}

    <div id={panelId+'-filters'} className="card-list-options" role="group" aria-label="筛选卡片" hidden={panel!=='filters'}>
      <div className="card-filters card-list-filters">
        <label>筛选状态<select value={status} onChange={e=>filters(()=>setStatus(e.target.value as typeof status))}><option value="">全部状态</option>{Object.entries(statusNames).map(([value,name])=><option key={value} value={value}>{name}</option>)}</select></label>
        <label>筛选类型<select value={kind} onChange={e=>filters(()=>setKind(e.target.value as typeof kind))}><option value="">全部类型</option>{Object.entries(kindNames).map(([value,name])=><option key={value} value={value}>{name}</option>)}</select></label>
      </div>
    </div>
    <section id={panelId+'-decks'} className="card-list-options" aria-label="管理卡片集" hidden={panel!=='decks'}>
      <form className="card-new-deck" onSubmit={e=>{e.preventDefault();void mutate(async()=>{await api('/learning/decks','POST',{name:newDeck});if(active())setNewDeck('')},'卡片集已创建。')}}>
        <label>新卡片集名称<input ref={newDeckInput} value={newDeck} onChange={e=>setNewDeck(e.target.value)} maxLength={100} placeholder="例如：高等数学" required/></label>
        <button className="secondary" disabled={busy||!newDeck.trim()}>创建卡片集</button>
      </form>
      {decks.isPending?<p role="status">正在读取卡片集…</p>:decks.data?.items.length?<div className="card-decks">{decks.data.items.map(deck=><DeckRow key={deck.id+':'+deck.revision} deck={deck} busy={busy} onPatch={patch=>void mutate(()=>api('/learning/decks/'+deck.id,'PATCH',{revision:deck.revision,...patch}),'卡片集已更新。')}/>)}</div>:null}
      {(decks.data?.total||0)>pageSize||deckOffset>0?<div className="card-pagination"><span>共 {decks.data?.total||0} 个卡片集</span><div><button type="button" className="text-button" disabled={!deckOffset||decks.isFetching} onClick={()=>setDeckOffset(value=>Math.max(0,value-pageSize))}>上一页卡片集</button><button type="button" className="text-button" disabled={deckOffset+pageSize>=(decks.data?.total||0)||decks.isFetching} onClick={()=>setDeckOffset(value=>value+pageSize)}>下一页卡片集</button></div></div>:null}
    </section>

    {currentPage.length?<div className={selected.length?'card-batch':'card-select-page'}>
      <label className="card-check"><input ref={selectPageInput} type="checkbox" aria-label="选择本页全部卡片" checked={allSelected} disabled={busy} onChange={e=>setSelected(e.target.checked?currentPage:[])}/>本页全选</label>
      {selected.length?<div className="card-batch-actions" role="group" aria-label="批量操作">
        <span role="status">已选 {selected.length} 张</span>
        <button type="button" className="secondary" disabled={busy} onClick={()=>review('confirm')}>明确确认所选</button>
        <button type="button" className="text-button" disabled={busy} onClick={()=>review('archive')}>归档所选</button>
        <button type="button" className="text-button" disabled={busy} onClick={()=>{setSelected([]);selectPageInput.current?.focus({preventScroll:true})}}>取消选择</button>
      </div>:null}
    </div>:null}
    {cards.isPending?<p role="status">正在读取卡片…</p>:currentPage.length?<ul className="learning-card-list">{currentPage.map(card=>{
      const checked=selected.some(item=>item.id===card.id)
      return <li key={card.id} className={checked?'is-selected':undefined}>
        <label className="card-row-check"><input type="checkbox" aria-label={'选择卡片 '+card.front.slice(0,30)} checked={checked} disabled={busy||selected.length>=pageSize&&!checked} onChange={e=>setSelected(old=>e.target.checked?[...old,card]:old.filter(item=>item.id!==card.id))}/></label>
        <button type="button" className="card-row-open" onClick={()=>setReading(current=>current===card.id?'':card.id)}>
          <strong>{card.front}</strong>
          <span><span className="badge">{kindNames[card.kind]}</span><span className={'badge '+card.status}>{statusNames[card.status]}</span>{card.generation_id?<span className="badge">AI 草稿来源</span>:null}</span>
          <small>{decks.data?.items.find(deck=>deck.id===card.deck_id)?.name||'卡片集'} · 版本 {card.revision}{card.source&&!card.source_valid?' · 来源需要核对':''}</small>
        </button>
        <button type="button" className="text-button" onClick={()=>{setShowGeneration(false);setEditor(card);setReading('')}}>编辑卡片</button>
      </li>
    })}</ul>:decks.data?.total===0&&panel!=='decks'?<div className="empty card-list-empty"><strong>先创建一个卡片集</strong><p>卡片集可以按学科或主题整理卡片。</p><button type="button" className="secondary" onClick={()=>setPanel('decks')}>新建卡片集</button></div>:<p className="empty">没有符合条件的卡片。创建卡片或调整筛选。</p>}
    <div className="card-pagination"><span>共 {cards.data?.total||0} 张{(cards.data?.total||0)>pageSize||offset>0?` · 第 ${Math.floor(offset/pageSize)+1} 页`:''}</span>{(cards.data?.total||0)>pageSize||offset>0?<div><button type="button" className="secondary" disabled={offset===0||cards.isFetching} onClick={()=>page(Math.max(0,offset-pageSize))}>上一页卡片</button><button type="button" className="secondary" disabled={offset+pageSize>=(cards.data?.total||0)||cards.isFetching} onClick={()=>page(offset+pageSize)}>下一页卡片</button></div>:null}</div>
  </section>
  {reading?<CardReader key={reading} owner={owner} sessionId={sessionId} id={reading} onOpenNote={onOpenNote} onClose={()=>setReading('')}/>:null}
  {editor?<CardEditor key={editor==='new'?'new':editor.id+':'+editor.revision} owner={owner} sessionId={sessionId} card={editor==='new'?undefined:editor} decks={decks.data?.items||[]} onOpenNote={onOpenNote} onClose={()=>setEditor(null)} onSaved={saved=>{setSelected(old=>old.map(item=>item.id===saved.id?saved:item));setEditor(null);setNotice('卡片已保存。');refresh()}}/>:null}
  {showGeneration?<CardGenerations owner={owner} sessionId={sessionId} decks={decks.data?.items||[]} onDrafts={refresh} onOpenCard={setReading}/>:null}
  </div>
}
function DeckRow({deck,busy,onPatch}:{deck:Deck;busy:boolean;onPatch:(patch:{name?:string;status?:Deck['status']})=>void}){
  const [name,setName]=useState(deck.name)
  return <form className="card-deck-row" onSubmit={e=>{e.preventDefault();onPatch({name})}}><label><span className="sr-only">重命名 {deck.name}</span><input aria-label={'重命名 '+deck.name} value={name} maxLength={100} onChange={e=>setName(e.target.value)} required/></label><span className="badge">{deck.status==='active'?'使用中':'已归档'}</span><button className="text-button" disabled={busy||name===deck.name||!name.trim()}>保存名称</button><button type="button" className="text-button" disabled={busy} onClick={()=>onPatch({status:deck.status==='active'?'archived':'active'})}>{deck.status==='active'?'归档卡片集':'恢复卡片集'}</button><small className="muted">归档保留全部卡片与历史</small></form>
}
function CardReader({owner,sessionId,id,onOpenNote,onClose}:{owner:string;sessionId:string;id:string;onOpenNote:(id:string)=>void;onClose:()=>void}){
  const card=useQuery({queryKey:['card',owner,sessionId,id],queryFn:()=>api<Card>('/learning/cards/'+id),retry:false}),[read,setRead]=useState<SourceRead|null>(null)
  const host=useRef<HTMLElement>(null)
  useLearningActivity('card_practice',card.data?.status==='confirmed'&&!card.isError?{card_id:id}:null)
  useEffect(()=>{host.current?.focus({preventScroll:true});host.current?.scrollIntoView({block:'start'})},[])
  const onRead=useCallback((value:SourceRead|null)=>setRead(value),[])
  return <section ref={host} tabIndex={-1} className="panel" aria-label="阅读卡片"><div className="panel-heading"><h3>阅读卡片</h3><button type="button" className="text-button" onClick={onClose}>关闭阅读</button></div><NoteError error={card.error}/>{card.isPending?<p role="status">正在读取卡片…</p>:card.data?<><CardPreview kind={card.data.kind} front={card.data.front} back={card.data.back} masks={card.data.payload?.masks} visual={read?sourceVisual(read):null}/><CardSource owner={owner} sessionId={sessionId} card={card.data} onRead={onRead} onOpenNote={onOpenNote}/></>:null}</section>
}
