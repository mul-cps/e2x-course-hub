import { useEffect, useState } from 'react';
import { config } from '../config';
import { requests } from '../api/client';

const kinds = ['courses', 'terms', 'memberships', 'groups', 'groupings', 'audit'];
export default function LocalRecordsPage() {
  const [kind, setKind] = useState('courses');
  const [records, setRecords] = useState<Record<string, unknown>[]>([]);
  const [draft, setDraft] = useState('{"id":"","name":""}');
  const [error, setError] = useState('');
  const url = `${config.apiUrl}/local/${kind}`;
  async function refresh() {
    try {
      const result = await requests.get(url) as { records: Record<string, unknown>[] };
      setRecords(result.records); setError('');
    } catch (err) { setError(String(err)); }
  }
  useEffect(() => {
    let active = true;
    requests.get(url).then(result => {
      if (active) { setRecords((result as {records: Record<string, unknown>[]}).records); setError(''); }
    }).catch(err => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [url]);
  async function save() {
    try { await requests.post(url, JSON.parse(draft)); await refresh(); }
    catch (err) { setError(String(err)); }
  }
  async function remove(id: unknown) {
    if (!window.confirm('Remove this local record? Referenced course records must be removed first.')) return;
    try { await requests.del(url, {id}); await refresh(); }
    catch (err) { setError(String(err)); }
  }
  return <section>
    <h2 className="text-xl font-semibold">Local course records</h2>
    <p>Administrator edits preserve identifiers and are audited. External records require a reviewed migration.</p>
    <label>Record type <select value={kind} onChange={e => setKind(e.target.value)}>
      {kinds.map(k => <option key={k}>{k}</option>)}
    </select></label>
    {error && <p role="alert">{error}</p>}
    <ul>{records.map((r,i) => <li key={String(r.id ?? i)} className="my-4">
      <pre className="whitespace-pre-wrap">{JSON.stringify(r,null,2)}</pre>
      {kind !== 'audit' && <><button onClick={() => setDraft(JSON.stringify(r,null,2))}>Edit</button>
        <button className="ml-4" onClick={() => remove(r.id)}>Remove</button></>}
    </li>)}</ul>
    {kind !== 'audit' && <><label className="block">Record JSON (id required; child records require course_id)
      <textarea className="block w-full border p-2" rows={8} value={draft} onChange={e => setDraft(e.target.value)} />
    </label><button onClick={save}>Save local record</button></>}
  </section>;
}
