import { useEffect, useState } from 'react';
import { config } from '../config';
import { requests } from '../api/client';

type Section = 'projects' | 'compute' | 'workspaces' | 'assignments' | 'audit';
const examples: Record<Section, Record<string, unknown>> = {
  projects: {id:'',name:'',description:''},
  compute: {id:'',person:'',profiles:[],projects:[],allowance:1,expires:'',reason:''},
  workspaces: {id:'',course_id:'',term_id:'',group_id:'',hub_user:'',hub_server:'',profile:'',course_ceiling:{cpu:'2',memory:'4Gi',gpuMemoryGiB:5}},
  assignments: {id:'',course_id:'',term_id:'',mode:'random',group_size:2,seed:'42'},
  audit: {},
};
export default function PlatformPage({section}: {section: Section}) {
  const [records,setRecords] = useState<Record<string,unknown>[]>([]);
  const [error,setError] = useState('');
  const [draft,setDraft] = useState<Record<string,unknown>>(examples[section]);
  const [person,setPerson] = useState('');
  const [loaded,setLoaded] = useState(false);
  const url = section === 'compute' ? `${config.apiUrl}/compute` : `${config.apiUrl}/local/${section}`;
  async function refresh() {
    try {
      const result = await requests.get(url,section==='compute' && person ? {person} : undefined) as Record<string,unknown>;
      const content = result.records ?? result.grants ?? result.profiles;
      setRecords(Array.isArray(content) ? content : [content as Record<string,unknown>]);
      setError('');setLoaded(true);
    } catch (err) { setError(String(err));setLoaded(true); }
  }
  useEffect(() => { setDraft(examples[section]);setLoaded(false);setRecords([]); },[section]);
  useEffect(() => {
    let active=true;
    requests.get(url).then(result => {
      if (!active) return;
      const data=result as Record<string,unknown>;
      const content=data.records ?? data.profiles;
      setRecords(Array.isArray(content) ? content : [content as Record<string,unknown>]);setError('');setLoaded(true);
    }).catch(err => { if(active){setError(String(err));setLoaded(true);} });
    return () => {active=false;};
  },[url]);
  async function save() {
    const target = section==='workspaces' ? `${config.apiUrl}/workspaces/create`
      : section==='assignments' ? `${config.apiUrl}/assignments/allocate` : url;
    try { await requests.post(target,draft); await refresh(); }
    catch(err){setError(String(err));}
  }
  async function action(id:unknown,action:string) {
    const membership_id = action==='remove-member' ? window.prompt('Membership identifier to remove. This stops the shared kernel and preserves files.') : null;
    if (action==='remove-member' && !membership_id) return;
    try { await requests.post(`${config.apiUrl}/workspaces/${encodeURIComponent(String(id))}/${action}`,membership_id ? {membership_id} : {});await refresh(); }
    catch(err){setError(String(err));}
  }
  function field(key:string,value:unknown) {
    if(typeof value==='number') return <input type="number" value={value} onChange={e=>setDraft({...draft,[key]:Number(e.target.value)})} />;
    if(Array.isArray(value)) return <input placeholder="Comma separated identifiers" value={value.join(',')} onChange={e=>setDraft({...draft,[key]:e.target.value.split(',').map(s=>s.trim()).filter(Boolean)})} />;
    if(value && typeof value==='object') return <fieldset><legend>{key}</legend>{Object.entries(value as Record<string,unknown>).map(([k,v])=><label className="block" key={k}>{k}<input value={String(v)} onChange={e=>setDraft({...draft,[key]:{...(value as Record<string,unknown>),[k]:typeof v==='number'?Number(e.target.value):e.target.value}})} /></label>)}</fieldset>;
    return <input value={String(value ?? '')} onChange={e=>setDraft({...draft,[key]:e.target.value})} />;
  }
  return <section>
    <h2 className="text-xl font-semibold capitalize">{section==='workspaces'?'Shared Workspaces':section}</h2>
    {section==='compute' && <><p>Time-bounded grants use shared policy. Profiles and ceilings are enforced by the compute service.</p><label>Canonical person<input value={person} onChange={e=>setPerson(e.target.value)} /></label><button onClick={refresh}>View grants</button></>}
    {section==='workspaces' && <p>Starting reserves one interactive allowance per member. Removing a member stops the shared kernel, revokes access and preserves files.</p>}
    {section==='assignments' && <p>CSV uses person_id,group_id columns. Manual mode accepts group to member lists. Random allocation uses an explicit seed and group size. Existing assignments require reviewed reallocation.</p>}
    {error && <p role="alert">{error}</p>}
    {!loaded && <p>Loading…</p>}
    <ul>{records.filter(Boolean).map((r,i)=><li key={String(r.id ?? i)} className="my-4">
      <pre className="whitespace-pre-wrap">{JSON.stringify(r,null,2)}</pre>
      {section==='assignments' && <button onClick={async()=>{try{await requests.post(`${config.apiUrl}/assignments/${encodeURIComponent(String(r.id))}/close`,{});await refresh();}catch(err){setError(String(err));}}}>Close assignment and stop writers</button>}
      {section==='projects' && <button onClick={()=>setDraft(r)}>Edit project</button>}
      {section==='workspaces' && <div className="flex gap-4">
        <button onClick={()=>action(r.id,'start')}>Start</button><button onClick={()=>action(r.id,'stop')}>Stop</button>
        <button onClick={()=>action(r.id,'remove-member')}>Remove member</button>
      </div>}
    </li>)}</ul>
    {section!=='audit' && <form onSubmit={e=>{e.preventDefault();void save();}}>
      <h3 className="font-semibold">{section==='compute'?'Add or replace grant':section==='assignments'?'Allocate assignment':'Create '+section}</h3>
      {Object.entries(draft).map(([key,value])=><label className="block my-2" key={key}>{key.replaceAll('_',' ')} {field(key,value)}</label>)}
      {section==='assignments' && <label className="block">CSV or manual allocation<textarea onChange={e=>{
        const value=e.target.value;
        try {setDraft({...draft,rows:draft.mode==='manual'?JSON.parse(value):value});}catch{setError('Manual allocation must be a JSON group to member list');}
      }} /></label>}
      <button type="submit">{section==='compute'?'Save grant':section==='assignments'?'Allocate groups':'Save'}</button>
    </form>}
  </section>;
}
