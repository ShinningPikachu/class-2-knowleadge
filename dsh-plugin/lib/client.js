window.__ModuleLoader__.load({ id: "dsh-class2knowledge", factory: (require) => {
  var module = { exports: {} }; var exports = module.exports;
const { createElement, useCallback, useEffect, useMemo, useRef, useState } = require("react");

const inject = ['slots'];
const ROUTE = '/api/class2knowledge';
const PANEL_ID = 'class2knowledge';
const h = createElement;
const C = {
  text: '#e2e8f0', muted: '#94a3b8', faint: '#64748b', accent: '#60a5fa',
  green: '#4ade80', amber: '#fbbf24', red: '#fca5a5', line: 'rgba(148,163,184,.18)',
  surface: 'rgba(15,23,42,.46)', selected: 'rgba(96,165,250,.18)'
};

async function api(endpoint, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), endpoint === 'ask' ? 200_000 : 20_000);
  let response;
  try {
    response = await fetch(ROUTE, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ endpoint, payload }), signal: controller.signal
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw new Error('The local request timed out. Check that Harness and the library are running.');
    throw new Error(error?.message || 'Could not connect to the Class Knowledge plugin.');
  } finally { clearTimeout(timer); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`The plugin returned an invalid response (HTTP ${response.status}).`); }
  if (!response.ok || data?.error) throw new Error(data?.error || `Plugin request failed (HTTP ${response.status}).`);
  return data;
}

const sections = [
  { id: 'library', label: 'Library', glyph: '▤' },
  { id: 'agent', label: 'Agent', glyph: '✦' },
  { id: 'lectureNotes', label: 'Lecture Notes', glyph: '♫' },
  { id: 'jobQueue', label: 'Job Queue', glyph: '☷' },
  { id: 'settings', label: 'Settings', glyph: '⚙' }
];

function resolveChatContext(selection, subject, selectedDocument) {
  let scope = selection;
  if (scope === 'current') scope = selectedDocument ? 'document' : subject ? 'subject' : 'all';
  if (scope === 'document' && !selectedDocument) scope = subject ? 'subject' : 'all';
  if (scope === 'subject' && !subject) scope = 'all';
  if (scope === 'document') return {
    scope, subject: selectedDocument.subject || subject,
    documentId: selectedDocument.id, label: `Document · ${selectedDocument.name}`
  };
  if (scope === 'subject') return { scope, subject, documentId: undefined, label: `Subject · ${subject}` };
  return { scope: 'all', subject: undefined, documentId: undefined, label: 'All subjects' };
}

const button = {
  padding: '7px 11px', borderRadius: 8, fontSize: 12, cursor: 'pointer',
  border: '1px solid ' + C.line, background: 'transparent', color: C.text, whiteSpace: 'nowrap'
};
const primaryButton = { ...button, borderColor: 'rgba(96,165,250,.45)', background: 'rgba(59,130,246,.15)' };
const pane = { display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' };
const scroll = { minHeight: 0, flex: 1, overflowY: 'auto', overflowX: 'hidden' };

function NavRow({ item, active, onClick }) {
  return h('button', {
    type: 'button', onClick, 'aria-current': active ? 'page' : undefined,
    style: {
      width: 'calc(100% - 16px)', margin: '2px 8px', padding: '9px 10px',
      display: 'flex', alignItems: 'center', gap: 11, textAlign: 'left',
      border: 0, borderRadius: 8, cursor: 'pointer', color: active ? '#fff' : C.text,
      background: active ? C.selected : 'transparent', fontSize: 13
    },
    onMouseEnter: event => { if (!active) event.currentTarget.style.background = 'rgba(148,163,184,.1)'; },
    onMouseLeave: event => { if (!active) event.currentTarget.style.background = 'transparent'; }
  },
    h('span', { 'aria-hidden': true, style: { width: 19, textAlign: 'center', fontSize: 16, color: active ? C.accent : C.muted } }, item.glyph),
    item.label
  );
}

function ListRow({ active, onClick, title, subtitle, glyph = '▤', badge }) {
  return h('button', {
    type: 'button', onClick, title,
    style: {
      width: 'calc(100% - 12px)', margin: '2px 6px', padding: '8px 9px',
      display: 'flex', alignItems: 'center', gap: 9, textAlign: 'left',
      border: 0, borderRadius: 7, cursor: 'pointer', color: C.text,
      background: active ? C.selected : 'transparent', fontSize: 12.5, minWidth: 0
    },
    onMouseEnter: event => { if (!active) event.currentTarget.style.background = 'rgba(148,163,184,.1)'; },
    onMouseLeave: event => { if (!active) event.currentTarget.style.background = 'transparent'; }
  },
    h('span', { 'aria-hidden': true, style: { color: active ? C.accent : C.muted, flex: 'none' } }, glyph),
    h('span', { style: { minWidth: 0, flex: 1 } },
      h('span', { style: { display: 'block', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' } }, title),
      subtitle ? h('span', { style: { display: 'block', color: C.muted, fontSize: 10.5, marginTop: 2 } }, subtitle) : null
    ),
    badge !== undefined ? h('span', { style: { color: C.muted, fontSize: 10.5, flex: 'none' } }, String(badge)) : null
  );
}

function PageHeader({ title, subtitle, action }) {
  return h('div', {
    style: { display: 'flex', alignItems: 'center', gap: 18, padding: '22px 24px 17px', borderBottom: '1px solid ' + C.line }
  },
    h('div', { style: { minWidth: 0, flex: 1 } },
      h('h1', { style: { margin: 0, fontSize: 22, fontWeight: 650, color: C.text } }, title),
      h('div', { style: { marginTop: 4, color: C.muted, fontSize: 12.5 } }, subtitle)
    ), action || null
  );
}

function Metric({ label, value, tone = C.text }) {
  return h('div', { style: { padding: '11px 13px', border: '1px solid ' + C.line, borderRadius: 9, background: C.surface, minWidth: 105 } },
    h('div', { style: { color: C.muted, fontSize: 11 } }, label),
    h('div', { style: { color: tone, fontSize: 20, fontWeight: 650, marginTop: 3 } }, String(value ?? 0))
  );
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function fileGlyph(name) {
  const ext = String(name || '').split('.').pop()?.toLowerCase();
  if (['mp3', 'm4a', 'wav', 'aac', 'flac', 'ogg', 'opus', 'mp4', 'mov', 'mkv', 'webm', 'm4v'].includes(ext)) return '♫';
  if (['ppt', 'pptx', 'key'].includes(ext)) return '▧';
  if (['md', 'markdown', 'txt'].includes(ext)) return '≡';
  return '▤';
}

function statusColor(status) {
  if (['running', 'waiting'].includes(status)) return C.accent;
  if (status === 'completed') return C.green;
  if (['failed', 'cancelled'].includes(status)) return C.red;
  if (['queued', 'deferred'].includes(status)) return C.amber;
  return C.muted;
}

function humanize(value) {
  return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase()) || 'Unknown';
}

function JobRow({ job }) {
  const active = ['running', 'waiting'].includes(job.status);
  return h('div', {
    style: { padding: '13px 15px', borderBottom: '1px solid ' + C.line, display: 'flex', gap: 13, alignItems: 'flex-start' }
  },
    h('span', { style: { color: statusColor(job.status), fontSize: 18, marginTop: 1 } }, job.kind === 'lecture' ? '♫' : '◷'),
    h('div', { style: { minWidth: 0, flex: 1 } },
      h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' } },
        h('strong', { style: { color: C.text, fontSize: 13 } }, job.title),
        h('span', { style: { color: statusColor(job.status), fontSize: 10.5, fontWeight: 650 } }, humanize(job.status)),
        h('span', { style: { color: C.faint, fontSize: 10.5 } }, humanize(job.kind))
      ),
      h('div', { style: { marginTop: 5, color: C.muted, fontSize: 11.5, lineHeight: 1.5 } }, job.message || job.stage || 'No additional details.'),
      active ? h('div', { style: { marginTop: 8, height: 4, borderRadius: 9, background: 'rgba(148,163,184,.16)', overflow: 'hidden' } },
        h('div', { style: { height: '100%', width: `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`, background: C.accent } })
      ) : null,
      active ? h('div', { style: { marginTop: 4, fontSize: 10.5, color: C.muted } }, `${Number(job.progress) || 0}% · ${humanize(job.stage)}`) : null,
      job.status === 'failed' && job.error ? h('div', { style: { marginTop: 5, color: C.red, fontSize: 11 } }, job.error) : null
    )
  );
}

function ClassKnowledgePage() {
  const [section, setSection] = useState('agent');
  const [status, setStatus] = useState(null);
  const [subjects, setSubjects] = useState([]);
  const [subject, setSubject] = useState('');
  const [folders, setFolders] = useState([]);
  const [folderId, setFolderId] = useState('root');
  const [documents, setDocuments] = useState([]);
  const [selectedDocument, setSelectedDocument] = useState(null);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [jobFilter, setJobFilter] = useState('active');
  const [showAllParts, setShowAllParts] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState('');
  const [action, setAction] = useState('');
  const [chatScope, setChatScope] = useState('current');
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState('');
  const [chatSending, setChatSending] = useState(false);
  const [chatError, setChatError] = useState('');
  const chatScrollRef = useRef(null);
  const libraryRequest = useRef(0);
  const searchRequest = useRef(0);
  const documentRequest = useRef(0);

  const loadLibrary = useCallback(async (nextSubject, nextFolder) => {
    const request = ++libraryRequest.current;
    if (!nextSubject) { setFolders([]); setDocuments([]); return; }
    setLoading('library');
    try {
      const [folderResult, documentResult] = await Promise.all([
        api('folders', { subject: nextSubject }),
        api('documents', { subject: nextSubject, folderId: nextFolder || 'root' })
      ]);
      if (request === libraryRequest.current) {
        setFolders(folderResult.folders || []);
        setDocuments(documentResult.documents || []);
      }
    } catch (cause) {
      if (request === libraryRequest.current) setError(String(cause?.message || cause));
    } finally {
      if (request === libraryRequest.current) setLoading('');
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading('refresh');
    setError('');
    try {
      const [nextStatus, subjectResult, jobResult] = await Promise.all([
        api('status'), api('subjects'),
        api('jobs').catch(cause => ({ jobs: [], error: String(cause?.message || cause) }))
      ]);
      const nextSubjects = subjectResult.subjects || [];
      const nextSubject = nextSubjects.some(item => item.subject === subject)
        ? subject : (nextSubjects[0]?.subject || '');
      const nextFolder = nextSubject === subject ? folderId : 'root';
      setStatus(nextStatus);
      setSubjects(nextSubjects);
      setJobs(jobResult.jobs || []);
      setFolderId(nextFolder);
      setSubject(nextSubject);
      if (!nextStatus.libraryAvailable) setError(nextStatus.libraryError || 'The library database is unavailable.');
      else if (nextStatus.jobsError || jobResult.error) setError(`Queue data is unavailable: ${nextStatus.jobsError || jobResult.error}`);
      await loadLibrary(nextSubject, nextFolder);
    } catch (cause) {
      setError(String(cause?.message || cause));
    } finally { setLoading(''); }
  }, [subject, folderId, loadLibrary]);

  useEffect(() => { void refresh(); }, []);

  const chooseSubject = useCallback(nextSubject => {
    searchRequest.current += 1;
    documentRequest.current += 1;
    setSubject(nextSubject);
    setFolderId('root');
    setQuery('');
    setHits(null);
    setSelectedDocument(null);
    setShowAllParts(false);
    void loadLibrary(nextSubject, 'root');
  }, [loadLibrary]);

  const chooseFolder = useCallback(nextFolder => {
    searchRequest.current += 1;
    documentRequest.current += 1;
    setFolderId(nextFolder);
    setQuery('');
    setHits(null);
    setSelectedDocument(null);
    void loadLibrary(subject, nextFolder);
  }, [loadLibrary, subject]);

  const openDocument = useCallback(async id => {
    const request = ++documentRequest.current;
    setLoading('document');
    setError('');
    try {
      const result = await api('document', { id });
      if (request === documentRequest.current) {
        setSelectedDocument(result);
        setShowAllParts(false);
      }
    } catch (cause) {
      if (request === documentRequest.current) setError(String(cause?.message || cause));
    } finally {
      if (request === documentRequest.current) setLoading('');
    }
  }, []);

  const runSearch = useCallback(async () => {
    const text = query.trim();
    if (!text) { setHits(null); return; }
    const request = ++searchRequest.current;
    setLoading('search');
    setError('');
    try {
      const result = await api('search', { query: text, subject: subject || undefined, limit: 30 });
      if (request === searchRequest.current) setHits(result.hits || []);
    } catch (cause) {
      if (request === searchRequest.current) setError(String(cause?.message || cause));
    } finally {
      if (request === searchRequest.current) setLoading('');
    }
  }, [query, subject]);

  const sendChatMessage = useCallback(async () => {
    const question = chatInput.trim();
    if (!question || chatSending) return;
    const context = resolveChatContext(chatScope, subject, selectedDocument);
    const history = chatMessages.slice(-8).map(message => ({
      role: message.role, content: message.content, context: message.context
    }));
    setChatMessages(current => [...current, {
      id: `user-${Date.now()}`, role: 'user', content: question, context: context.label
    }]);
    setChatInput('');
    setChatError('');
    setChatSending(true);
    try {
      const result = await api('ask', {
        question, subject: context.subject, documentId: context.documentId, history
      });
      setChatMessages(current => [...current, {
        id: `assistant-${Date.now()}`, role: 'assistant', content: result.answer,
        sources: result.sources || [], context: context.label
      }]);
    } catch (cause) {
      setChatError(String(cause?.message || cause));
    } finally { setChatSending(false); }
  }, [chatInput, chatSending, chatScope, subject, selectedDocument, chatMessages]);

  const clearChat = useCallback(() => {
    if (chatSending) return;
    setChatMessages([]);
    setChatError('');
  }, [chatSending]);

  useEffect(() => {
    const element = chatScrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [chatMessages, chatSending, section]);

  const runAction = useCallback(async actionName => {
    if (actionName === 'stop' && !window.confirm('Quit Class Knowledge? This pauses any local task currently running in the app.')) return;
    setAction(actionName);
    setError('');
    try {
      const result = await api(actionName);
      await refresh();
      if (result.warning) setError(result.warning);
      else if (actionName === 'stop' && !result.stopped && result.wasRunning) setError('The app did not exit yet.');
    } catch (cause) { setError(String(cause?.message || cause)); }
    finally { setAction(''); }
  }, [refresh]);

  const totalDocuments = useMemo(
    () => subjects.reduce((sum, item) => sum + Number(item.documents || 0), 0), [subjects]
  );
  const queueCounts = status?.queueCounts || {};
  const activeJobs = Number(queueCounts.running || 0) + Number(queueCounts.waiting || 0);
  const appState = status?.running ? 'Application running' : status?.appInstalled ? 'Application ready' : 'Application not installed';

  const appHeader = h('header', {
    style: { display: 'flex', alignItems: 'center', gap: 14, padding: '11px 17px', borderBottom: '1px solid ' + C.line, flex: 'none', flexWrap: 'wrap' }
  },
    h('div', { style: { fontSize: 15, fontWeight: 650, color: C.text } }, 'Class Knowledge'),
    h('div', { style: { color: C.muted, fontSize: 11.5 } }, `${subjects.length} subjects · ${totalDocuments} files`),
    h('div', { style: { flex: 1, minWidth: 8 } }),
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, color: C.muted, fontSize: 11.5 } },
      h('span', { style: { width: 7, height: 7, borderRadius: 9, background: status?.running ? C.green : status?.appInstalled ? C.amber : C.red } }),
      appState
    ),
    h('button', { type: 'button', style: button, disabled: loading === 'refresh' || !!action, onClick: () => void refresh() }, loading === 'refresh' ? 'Refreshing…' : 'Refresh'),
    h('button', { type: 'button', style: primaryButton, disabled: !!action, onClick: () => void runAction('start') }, action === 'start' ? 'Opening…' : 'Open Class Knowledge')
  );

  const nav = h('aside', {
    style: { ...pane, width: 195, flex: 'none', borderRight: '1px solid ' + C.line, background: 'rgba(15,23,42,.2)' }
  },
    h('div', { style: { padding: '18px 16px 7px', color: C.muted, fontSize: 10.5, fontWeight: 650, letterSpacing: 1 } }, 'STUDY TOOLS'),
    h('nav', { style: { paddingTop: 2 } }, sections.map(item => h(NavRow, {
      key: item.id, item, active: section === item.id, onClick: () => setSection(item.id)
    }))),
    h('div', { style: { flex: 1 } }),
    h('div', { style: { borderTop: '1px solid ' + C.line, padding: '13px 16px 17px' } },
      h('div', { style: { color: C.muted, fontSize: 10.5, fontWeight: 650, letterSpacing: .7, marginBottom: 9 } }, 'QUEUE'),
      queueMetric('Active', activeJobs, C.accent),
      queueMetric('Queued', queueCounts.queued, C.amber),
      queueMetric('Later', queueCounts.deferred, C.muted)
    )
  );

  const libraryView = libraryPage({
    subjects, subject, folders, folderId, documents, selectedDocument, query, hits,
    loading, setQuery, chooseSubject, chooseFolder, runSearch, openDocument,
    showAllParts, setShowAllParts
  });

  let content;
  switch (section) {
    case 'agent': content = agentPage({
      status, subjects, subject, selectedDocument, chatScope, setChatScope,
      chatMessages, chatInput, setChatInput, chatSending, chatError,
      sendChatMessage, clearChat, chatScrollRef
    }); break;
    case 'lectureNotes': content = lecturePage(jobs, status, runAction); break;
    case 'jobQueue': content = jobsPage(jobs, queueCounts, jobFilter, setJobFilter, loading); break;
    case 'settings': content = settingsPage(status, runAction, action); break;
    default: content = libraryView;
  }

  return h('div', { style: { display: 'flex', flexDirection: 'column', height: '100%', width: '100%', minHeight: 0, color: C.text, overflow: 'hidden' } },
    appHeader,
    error ? h('div', { role: 'alert', style: { display: 'flex', gap: 10, alignItems: 'center', padding: '7px 14px', fontSize: 11.5, color: C.red, background: 'rgba(248,113,113,.1)' } },
      h('span', { style: { flex: 1 } }, error),
      h('button', { type: 'button', 'aria-label': 'Dismiss', onClick: () => setError(''), style: { ...button, padding: '3px 8px', color: C.red } }, 'Dismiss')
    ) : null,
    h('div', { style: { display: 'flex', flex: 1, minHeight: 0 } }, nav,
      h('main', { style: { display: 'flex', flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' } }, content)
    )
  );
}

function queueMetric(label, value, color) {
  return h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 6, color: C.muted, fontSize: 11.5 } },
    h('span', null, label), h('strong', { style: { color: color || C.text, fontWeight: 600 } }, String(value || 0))
  );
}

function libraryPage(data) {
  const {
    subjects, subject, folders, folderId, documents, selectedDocument, query, hits,
    loading, setQuery, chooseSubject, chooseFolder, runSearch, openDocument,
    showAllParts, setShowAllParts
  } = data;
  const visibleParts = selectedDocument?.parts || [];
  const shownParts = showAllParts ? visibleParts : visibleParts.slice(0, 40);
  const subjectTotal = subjects.find(item => item.subject === subject)?.documents ?? 0;
  const subjectPane = h('section', { style: { ...pane, width: 210, flex: 'none', borderRight: '1px solid ' + C.line } },
    h('div', { style: { padding: '15px 13px 8px', fontSize: 13, fontWeight: 600, color: C.text } }, 'Subjects'),
    h('div', { style: scroll }, subjects.length ? subjects.map(item => h(ListRow, {
      key: item.subject, active: subject === item.subject,
      title: item.subject, glyph: '▤', badge: item.documents,
      onClick: () => chooseSubject(item.subject)
    })) : h(EmptyState, { title: 'No subjects yet', detail: 'Create a subject in Class Knowledge to start organizing course files.' }))
  );

  const title = folderId === 'root' ? (subject || 'Library') : (folders.find(item => item.id === folderId)?.name || 'Folder');
  const filePane = h('section', { style: { ...pane, width: 315, flex: 'none', borderRight: '1px solid ' + C.line } },
    h('div', { style: { padding: '14px 13px 10px', borderBottom: '1px solid ' + C.line } },
      h('div', { style: { fontSize: 14, fontWeight: 650, color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, title),
      h('div', { style: { color: C.muted, fontSize: 10.5, marginTop: 3 } }, `${subjectTotal} files · ${folders.length} folders`),
      h('div', { style: { display: 'flex', gap: 6, marginTop: 11 } },
        h('input', {
          value: query, onChange: event => setQuery(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') void runSearch(); if (event.key === 'Escape') { setQuery(''); chooseFolder(folderId); } },
          placeholder: `Search ${subject || 'library'}`, 'aria-label': 'Search library',
          style: { width: '100%', minWidth: 0, padding: '7px 9px', borderRadius: 7, border: '1px solid ' + C.line, color: C.text, background: 'rgba(15,23,42,.7)', fontSize: 11.5 }
        }),
        h('button', { type: 'button', title: 'Search extracted text', 'aria-label': 'Search', style: { ...button, padding: '6px 9px' }, onClick: () => void runSearch() }, '⌕')
      ),
      hits !== null ? h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8, color: C.muted, fontSize: 10.5 } },
        h('span', null, `${hits.length} search results`),
        h('button', { type: 'button', onClick: () => { setQuery(''); data.chooseFolder(folderId); }, style: { border: 0, background: 'transparent', color: C.accent, cursor: 'pointer', fontSize: 10.5 } }, 'Clear')
      ) : null
    ),
    h('div', { style: scroll },
      hits !== null
        ? hits.length ? hits.map((hit, index) => h('button', {
            key: `${hit.id}:${hit.locator}:${index}`, type: 'button', onClick: () => void openDocument(hit.id),
            style: { display: 'block', width: '100%', textAlign: 'left', padding: '10px 13px', border: 0, borderBottom: '1px solid ' + C.line, background: 'transparent', cursor: 'pointer' }
          },
            h('div', { style: { color: C.text, fontSize: 11.5, fontWeight: 600 } }, hit.doc),
            h('div', { style: { color: C.accent, fontSize: 10.5, marginTop: 3 } }, `${hit.subject} · ${hit.locator || 'Full text'}`),
            h('div', { style: { color: C.muted, fontSize: 10.5, lineHeight: 1.5, marginTop: 5 } }, hit.snippet)
          )) : h(EmptyState, { title: 'No matches', detail: 'No indexed passages matched this search.' })
        : h('div', null,
            folderId !== 'root' ? h(ListRow, { title: 'Subject root', glyph: '↶', onClick: () => chooseFolder('root') }) : null,
            folderId === 'root' ? folders.map(item => h(ListRow, {
              key: item.id, title: item.name, glyph: '▱', badge: item.documentCount,
              onClick: () => chooseFolder(item.id)
            })) : null,
            documents.map(item => h(ListRow, {
              key: item.id, title: item.name,
              subtitle: `${formatBytes(item.sizeBytes)} · ${humanize(item.status)}`,
              glyph: fileGlyph(item.name), active: selectedDocument?.id === item.id,
              onClick: () => void openDocument(item.id)
            })),
            !folders.length && !documents.length ? h(EmptyState, {
              title: loading === 'library' ? 'Loading files…' : folderId === 'root' ? 'No files yet' : 'This folder is empty',
              detail: 'Import and organize course materials in the native Class Knowledge app.'
            }) : null
          )
    )
  );

  const previewPane = h('section', { style: { ...pane, flex: 1, minWidth: 280 } },
    selectedDocument ? h('div', { style: { ...pane, height: '100%' } },
      h('div', { style: { padding: '15px 18px 12px', borderBottom: '1px solid ' + C.line } },
        h('div', { style: { fontSize: 15, fontWeight: 650, color: C.text } }, selectedDocument.name),
        h('div', { style: { color: C.muted, fontSize: 10.5, marginTop: 4 } },
          [selectedDocument.subject, selectedDocument.folderName].filter(Boolean).join(' / ') || 'Library root',
          ' · ', formatBytes(selectedDocument.sizeBytes), ' · ', humanize(selectedDocument.status)
        ),
        selectedDocument.parts.length > 40 ? h('div', { style: { marginTop: 8, color: C.muted, fontSize: 10.5 } },
          `${showAllParts ? selectedDocument.parts.length : 40} of ${selectedDocument.parts.length} text sections`
        ) : null
      ),
      h('div', { style: { ...scroll, padding: '15px 20px 30px' } },
        selectedDocument.parts.length
          ? shownParts.map((part, index) => h('article', { key: `${part.locator}:${index}`, style: { marginBottom: 19 } },
              part.locator ? h('div', { style: { color: C.accent, fontSize: 10.5, fontWeight: 600, marginBottom: 5 } }, part.locator) : null,
              h('div', { style: { color: C.text, fontSize: 12.5, lineHeight: 1.75, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, part.content)
            ))
          : h(EmptyState, { title: 'No extracted text', detail: 'This file is stored in the library but has no searchable text preview.' }),
        selectedDocument.parts.length > 40 && !showAllParts ? h('button', {
          type: 'button', style: button, onClick: () => setShowAllParts(true)
        }, `Show all ${selectedDocument.parts.length} sections`) : null
      )
    ) : h(EmptyState, {
      title: loading === 'document' ? 'Opening file…' : 'Select a file',
      detail: 'Choose a file from the list to see its extracted text and source locations.'
    })
  );

  return h('div', { style: { display: 'flex', flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' } }, subjectPane, filePane, previewPane);
}

function EmptyState({ title, detail }) {
  return h('div', { style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 7, padding: 24, minHeight: 110, color: C.muted, textAlign: 'center' } },
    h('div', { style: { fontSize: 13, fontWeight: 600, color: C.text } }, title),
    h('div', { style: { fontSize: 11.5, lineHeight: 1.55, maxWidth: 260 } }, detail)
  );
}

function agentPage(data) {
  const {
    status, subject, selectedDocument, chatScope, setChatScope, chatMessages,
    chatInput, setChatInput, chatSending, chatError, sendChatMessage,
    clearChat, chatScrollRef
  } = data;
  const context = resolveChatContext(chatScope, subject, selectedDocument);
  const bridgeOnline = !!status?.agentBridgeAvailable;
  return h('div', { style: { ...pane, flex: 1, minWidth: 0 } },
    h(PageHeader, {
      title: 'Study Chat',
      subtitle: 'Ask about your courses. Answers combine matching files with the active Harness model’s knowledge.',
      action: h('button', { type: 'button', style: button, disabled: chatSending || !chatMessages.length, onClick: clearChat }, 'New chat')
    }),
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, padding: '9px 20px', borderBottom: '1px solid ' + C.line, color: C.muted, fontSize: 11.5, flex: 'none' } },
      h('span', { style: { width: 7, height: 7, borderRadius: 9, background: bridgeOnline ? C.green : C.amber } }),
      h('span', null, bridgeOnline ? 'Harness model ready' : 'Harness agent bridge unavailable'),
      h('span', { style: { color: C.faint } }, '·'),
      h('span', { style: { color: C.text } }, context.label),
      h('span', { style: { flex: 1 } }),
      h('span', null, `${status?.documents || 0} library files`)
    ),
    h('div', { ref: chatScrollRef, style: { ...scroll, padding: '22px clamp(16px, 4vw, 54px)', display: 'flex', flexDirection: 'column', gap: 14 } },
      chatMessages.length
        ? chatMessages.map(message => h(ChatMessageRow, { key: message.id, message }))
        : h('div', { style: { margin: 'auto', maxWidth: 470 } },
            h(EmptyState, {
              title: 'What would you like to understand?',
              detail: `I’ll look for useful passages in ${context.label.toLowerCase()}, cite the files I use, and fill gaps with the Harness model’s general knowledge.`
            }),
            h('div', { style: { display: 'flex', flexWrap: 'wrap', justifyContent: 'center', gap: 7, marginTop: 4 } },
              ['Explain a key idea', 'Summarize this file', 'Compare two concepts'].map(text => h('button', {
                key: text, type: 'button', onClick: () => setChatInput(text),
                style: { ...button, color: C.muted, fontSize: 10.5 }
              }, text))
            )
          ),
      chatSending ? h('div', { role: 'status', style: { display: 'flex', gap: 9, alignItems: 'center', color: C.muted, fontSize: 12, padding: '8px 2px' } },
        h('span', { style: { color: C.accent } }, '✦'), 'Searching the library and preparing an answer…'
      ) : null,
      chatError ? h('div', { role: 'alert', style: { padding: 10, borderRadius: 8, background: 'rgba(248,113,113,.1)', color: C.red, fontSize: 11.5 } }, chatError) : null
    ),
    h('form', {
      onSubmit: event => { event.preventDefault(); void sendChatMessage(); },
      style: { borderTop: '1px solid ' + C.line, padding: '12px clamp(16px, 4vw, 54px) 15px', flex: 'none' }
    },
      h('div', { style: { display: 'flex', gap: 12, alignItems: 'center', marginBottom: 8 } },
        h('label', { htmlFor: 'classlib-chat-scope', style: { color: C.muted, fontSize: 10.5 } }, 'Answer from'),
        h('select', {
          id: 'classlib-chat-scope', value: chatScope, onChange: event => setChatScope(event.target.value),
          style: { maxWidth: 'min(420px, 65%)', minWidth: 150, padding: '5px 8px', borderRadius: 7, border: '1px solid ' + C.line, color: C.text, background: '#111827', fontSize: 10.5 }
        },
          h('option', { value: 'current' }, `Current selection · ${selectedDocument?.name || subject || 'All subjects'}`),
          h('option', { value: 'subject', disabled: !subject }, `Selected subject · ${subject || 'Choose a subject in Library'}`),
          h('option', { value: 'document', disabled: !selectedDocument }, `Selected document · ${selectedDocument?.name || 'Choose a file in Library'}`),
          h('option', { value: 'all' }, 'All subjects')
        ),
        h('span', { style: { color: C.faint, fontSize: 10.5 } }, 'You can change the selection in Library.')
      ),
      h('div', { style: { display: 'flex', gap: 9, alignItems: 'stretch', padding: 9, border: '1px solid ' + C.line, borderRadius: 10, background: C.surface } },
        h('textarea', {
          value: chatInput, onChange: event => setChatInput(event.target.value),
          onKeyDown: event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent?.isComposing) {
              event.preventDefault(); void sendChatMessage();
            }
          },
          rows: 3, maxLength: 2000, placeholder: 'Ask a question about a subject or document…',
          'aria-label': 'Ask Class Knowledge',
          style: { flex: 1, minWidth: 0, resize: 'vertical', border: 0, outline: 'none', color: C.text, background: 'transparent', font: 'inherit', fontSize: 12, lineHeight: 1.6, padding: '6px 8px' }
        }),
        h('button', {
          type: 'submit', disabled: chatSending || !chatInput.trim() || !bridgeOnline,
          style: { ...primaryButton, minWidth: 82, alignSelf: 'flex-end', opacity: chatSending || !chatInput.trim() || !bridgeOnline ? .55 : 1 }
        }, chatSending ? 'Thinking…' : 'Ask')
      ),
      h('div', { style: { color: C.faint, fontSize: 10, marginTop: 6 } },
        bridgeOnline ? 'Enter to ask · Shift + Enter for a new line · Local source citations appear with each answer.' : 'Enable the Harness agent bridge to ask questions here.'
      )
    )
  );
}

function ChatMessageRow({ message }) {
  const isUser = message.role === 'user';
  return h('article', { style: { maxWidth: isUser ? 'min(82%, 720px)' : '100%', alignSelf: isUser ? 'flex-end' : 'stretch' } },
    h('div', {
      style: {
        padding: isUser ? '10px 13px' : '2px 1px', borderRadius: 10,
        background: isUser ? 'rgba(59,130,246,.15)' : 'transparent',
        border: isUser ? '1px solid rgba(96,165,250,.18)' : 0,
        color: C.text, fontSize: 12.5, lineHeight: 1.75, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'
      }
    }, message.content),
    isUser ? h('div', { style: { color: C.faint, textAlign: 'right', fontSize: 9.5, marginTop: 3 } }, message.context) : null,
    !isUser && message.sources?.length ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 5, marginTop: 10 } },
      message.sources.map((source, index) => h('details', {
        key: `${source.id}:${source.locator}:${index}`,
        style: { border: '1px solid ' + C.line, borderRadius: 7, padding: '6px 9px', background: 'rgba(15,23,42,.25)' }
      },
        h('summary', { style: { cursor: 'pointer', color: C.accent, fontSize: 10.5 } },
          `[S${index + 1}] ${source.doc}${source.locator ? ` · ${source.locator}` : ''}`
        ),
        h('div', { style: { color: C.muted, fontSize: 10, marginTop: 6 } },
          [source.subject, source.folderName].filter(Boolean).join(' · ')
        ),
        h('div', { style: { color: C.text, fontSize: 10.5, lineHeight: 1.6, marginTop: 5, whiteSpace: 'pre-wrap' } }, source.snippet)
      ))
    ) : null
  );
}

function lecturePage(jobs, status, runAction) {
  const lectures = jobs.filter(job => job.kind === 'lecture');
  return h('div', { style: { ...pane, flex: 1 } },
    h(PageHeader, { title: 'Lecture Notes', subtitle: 'Queue a recording, slides, or both. Processing runs locally in the background.', action: h('button', { type: 'button', style: primaryButton, onClick: () => void runAction('start') }, 'Open Class Knowledge') }),
    h('div', { style: { ...scroll, padding: 23 } },
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(190px,1fr))', gap: 11, marginBottom: 22 } },
        h(InfoCard, { title: '1 · Choose sources', detail: 'Select a recording or a slide deck from your Mac.' }),
        h(InfoCard, { title: '2 · Set destination', detail: 'Add a title and choose the subject where the materials belong.' }),
        h(InfoCard, { title: '3 · Track progress', detail: 'The queue saves checkpoints so interrupted work can resume.' })
      ),
      h('div', { style: { fontSize: 15, fontWeight: 650, color: C.text, marginBottom: 8 } }, 'Recent lecture tasks'),
      h('div', { style: { border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden' } },
        lectures.length ? lectures.slice(0, 50).map(job => h(JobRow, { key: job.id, job })) : h(EmptyState, {
          title: status?.jobsAvailable === false ? 'Queue is not initialized yet' : 'No lecture tasks yet',
          detail: 'Create a lecture task in Class Knowledge. Its status and checkpoints will appear here.'
        })
      )
    )
  );
}

function jobsPage(jobs, counts, filter, setFilter, loading) {
  const filters = [
    { id: 'active', label: 'Active', count: Number(counts.running || 0) + Number(counts.waiting || 0) },
    { id: 'queued', label: 'Queued', count: counts.queued || 0 },
    { id: 'later', label: 'Later', count: counts.deferred || 0 },
    { id: 'history', label: 'History', count: Number(counts.completed || 0) + Number(counts.failed || 0) + Number(counts.cancelled || 0) }
  ];
  const visible = jobs.filter(job => {
    if (filter === 'active') return ['running', 'waiting'].includes(job.status);
    if (filter === 'queued') return job.status === 'queued';
    if (filter === 'later') return job.status === 'deferred';
    return ['completed', 'failed', 'cancelled'].includes(job.status);
  });
  const empty = {
    active: ['No active tasks', 'Tasks appear here when local processing begins.'],
    queued: ['Queue is clear', 'New lecture, translation, and review tasks will appear here.'],
    later: ['Nothing saved for later', 'Deferred tasks keep their checkpoints for resuming.'],
    history: ['No task history yet', 'Completed and failed work will appear here.']
  }[filter];
  return h('div', { style: { ...pane, flex: 1 } },
    h(PageHeader, { title: 'Job Queue', subtitle: 'Tasks run by priority and preserve checkpoints for safe recovery.' }),
    h('div', { style: { ...scroll, padding: 23 } },
      h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 20 } },
        h(Metric, { label: 'Queued', value: counts.queued }),
        h(Metric, { label: 'Active', value: Number(counts.running || 0) + Number(counts.waiting || 0), tone: C.accent }),
        h(Metric, { label: 'Later', value: counts.deferred, tone: C.amber }),
        h(Metric, { label: 'Completed', value: counts.completed, tone: C.green }),
        h(Metric, { label: 'Failed', value: counts.failed, tone: C.red })
      ),
      h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6, paddingBottom: 13, borderBottom: '1px solid ' + C.line } },
        filters.map(item => h('button', {
          key: item.id, type: 'button', onClick: () => setFilter(item.id),
          style: { ...button, background: filter === item.id ? C.selected : 'transparent', borderColor: filter === item.id ? 'rgba(96,165,250,.38)' : C.line }
        }, `${item.label} (${item.count})`))
      ),
      h('div', { style: { marginTop: 12, border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden' } },
        visible.length ? visible.map(job => h(JobRow, { key: job.id, job })) : h(EmptyState, {
          title: loading === 'refresh' ? 'Refreshing queue…' : empty[0], detail: empty[1]
        })
      )
    )
  );
}

function settingsPage(status, runAction, action) {
  const config = status?.nativeSettings || {};
  return h('div', { style: { ...pane, flex: 1 } },
    h(PageHeader, { title: 'Settings', subtitle: 'Workspace, local services, and model configuration.' }),
    h('div', { style: { ...scroll, padding: 23, maxWidth: 1000 } },
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 12 } },
        h(InfoCard, { title: 'Workspace', detail: status?.root || 'Workspace path unavailable.' },
          h('div', { style: { color: C.muted, fontSize: 11, marginTop: 8 } }, 'Library files and queue state are stored locally in this project.')
        ),
        h(InfoCard, { title: 'Library database', detail: status?.libraryAvailable ? 'Available' : 'Unavailable', tone: status?.libraryAvailable ? C.green : C.red },
          h('div', { style: { color: C.muted, fontSize: 11, marginTop: 8 } }, `${status?.documents || 0} documents · ${status?.chunks || 0} searchable text sections`)
        ),
        h(InfoCard, { title: 'Native application', detail: status?.appInstalled ? (status.running ? 'Running' : 'Installed') : 'Not installed', tone: status?.running ? C.green : C.amber },
          h('div', { style: { color: C.muted, fontSize: 11, marginTop: 8 } }, 'The app runs its Python service locally over JSONL on stdio.')
        ),
        h(InfoCard, { title: 'Harness agent bridge', detail: status?.agentBridgeAvailable ? 'Available' : 'Unavailable', tone: status?.agentBridgeAvailable ? C.green : C.amber },
          h('div', { style: { color: C.muted, fontSize: 11, marginTop: 8 } }, 'Private Unix socket · health checks do not create model turns.')
        )
      ),
      h('div', { style: { marginTop: 19 } },
        h('div', { style: { fontSize: 15, fontWeight: 650, color: C.text, marginBottom: 8 } }, 'Current app configuration'),
        h('div', { style: { border: '1px solid ' + C.line, borderRadius: 10, padding: '4px 14px' } },
          settingRow('Library agent provider', humanize(config.agentProvider || 'ollama')),
          settingRow('Language model', config.llmModel || 'Not configured'),
          settingRow('Embedding model', config.embeddingModel || 'Not configured'),
          settingRow('Recording model', config.whisperModel || 'Not configured'),
          settingRow('Lecture language', config.language || 'English')
        )
      ),
      h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 18 } },
        h('button', { type: 'button', style: primaryButton, disabled: !!action, onClick: () => void runAction('start') }, action === 'start' ? 'Opening…' : 'Open Class Knowledge'),
        status?.running ? h('button', { type: 'button', style: { ...button, color: C.red }, disabled: !!action, onClick: () => void runAction('stop') }, action === 'stop' ? 'Quitting…' : 'Quit Class Knowledge…') : null
      ),
      h('div', { style: { marginTop: 9, color: C.muted, fontSize: 10.5 } }, 'Settings are managed in the native app. The plugin displays a safe summary and does not edit configuration.')
    )
  );
}

function InfoCard({ title, detail, tone = C.text, children }) {
  return h('section', { style: { padding: 15, border: '1px solid ' + C.line, borderRadius: 10, background: C.surface, minWidth: 0 } },
    h('div', { style: { color: C.muted, fontSize: 11, fontWeight: 600 } }, title),
    detail ? h('div', { style: { color: tone, fontSize: 13, fontWeight: 600, lineHeight: 1.5, marginTop: 6, overflowWrap: 'anywhere' } }, detail) : null,
    children || null
  );
}

function settingRow(label, value) {
  return h('div', { style: { display: 'flex', gap: 15, justifyContent: 'space-between', padding: '10px 0', borderBottom: '1px solid ' + C.line, fontSize: 11.5 } },
    h('span', { style: { color: C.muted } }, label),
    h('span', { style: { color: C.text, textAlign: 'right' } }, String(value))
  );
}

function toolChip(name, detail) {
  return h('div', { key: name, style: { display: 'flex', gap: 8, alignItems: 'center', padding: '6px 9px', borderRadius: 7, background: 'rgba(96,165,250,.1)', color: C.text, fontSize: 10.5 } },
    h('code', { style: { color: C.accent } }, name), h('span', { style: { color: C.muted } }, detail)
  );
}

function apply(ctx) {
  const slots = ctx.slots;
  if (!slots) return;
  slots.inject('main', () => slots.register({ name: 'main', key: PANEL_ID }, ClassKnowledgePage));
  slots.inject('sidebar.panellist', () => slots.register({
    name: 'sidebar.panellist', id: PANEL_ID, order: 30, label: () => 'Class Knowledge'
  }, ({ size = 20 }) => h('svg', {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true
  },
    h('path', { d: 'M4 5.5A1.5 1.5 0 0 1 5.5 4H9v16H5.5A1.5 1.5 0 0 1 4 18.5z' }),
    h('path', { d: 'M9 4h4.5A1.5 1.5 0 0 1 15 5.5v13a1.5 1.5 0 0 1-1.5 1.5H9z' }),
    h('path', { d: 'M17 6.5 20 7l-2.5 12-3-1' })
  )));
}

module.exports = { apply, inject };
return module.exports;
} });
