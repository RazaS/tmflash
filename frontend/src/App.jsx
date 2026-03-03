import { useEffect, useMemo, useState } from 'react'

const TABS = ['Study', 'Resources', 'Import', 'Draft Review', 'Progress']

async function api(path, options = {}) {
  const opts = { credentials: 'include', ...options }
  const headers = new Headers(options.headers || {})
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  opts.headers = headers
  const res = await fetch(path, opts)
  const data = await res.json().catch(() => ({ ok: false, message: 'Invalid server response.' }))
  if (!res.ok && data.ok !== false) {
    return { ok: false, message: data.message || `HTTP ${res.status}` }
  }
  return data
}

function AuthPanel({ onAuth }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')

  async function submit(e) {
    e.preventDefault()
    setMessage('')
    const result = await api(`/api/${mode === 'login' ? 'login' : 'signup'}`, {
      method: 'POST',
      body: JSON.stringify({ username, password })
    })
    if (!result.ok) {
      setMessage(result.message || 'Authentication failed.')
      return
    }
    onAuth()
  }

  return (
    <div className="auth-card">
      <h1>Flashcard Studio</h1>
      <p>Upload resources, review draft cards, and study published decks.</p>
      <form onSubmit={submit}>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} minLength={3} required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={6} required />
        </label>
        <button type="submit">{mode === 'login' ? 'Log In' : 'Create Account'}</button>
      </form>
      <button className="link" onClick={() => setMode(mode === 'login' ? 'signup' : 'login')}>
        {mode === 'login' ? 'Need an account? Sign up.' : 'Already have an account? Log in.'}
      </button>
      {message ? <p className="error">{message}</p> : null}
    </div>
  )
}

function ResourcesTab({ resources }) {
  const [selectedResource, setSelectedResource] = useState(null)
  const [versions, setVersions] = useState([])
  const [loadingVersions, setLoadingVersions] = useState(false)

  async function openResource(resourceId) {
    setSelectedResource(resourceId)
    setLoadingVersions(true)
    const data = await api(`/api/resources/${resourceId}/versions`)
    setVersions(data.ok ? data.versions : [])
    setLoadingVersions(false)
  }

  return (
    <section className="panel">
      <h2>Resource Library</h2>
      <div className="two-col">
        <div>
          <h3>Resources</h3>
          <ul className="list">
            {resources.map((r) => (
              <li key={r.id}>
                <button className="list-item" onClick={() => openResource(r.id)}>
                  <strong>{r.title}</strong>
                  <span>{r.source_type.toUpperCase()} · {r.version_count} versions</span>
                </button>
              </li>
            ))}
            {resources.length === 0 ? <li>No resources yet.</li> : null}
          </ul>
        </div>
        <div>
          <h3>Versions {selectedResource ? `(Resource ${selectedResource})` : ''}</h3>
          {loadingVersions ? <p>Loading versions...</p> : null}
          <ul className="list">
            {versions.map((v) => (
              <li key={v.id} className="card-mini">
                <strong>{v.version_label}</strong>
                <span>Status: {v.status}</span>
                <span>Cards: {v.card_count}</span>
              </li>
            ))}
            {!loadingVersions && versions.length === 0 ? <li>Select a resource to view versions.</li> : null}
          </ul>
        </div>
      </div>
    </section>
  )
}

function ImportTab({ onImported }) {
  const [file, setFile] = useState(null)
  const [title, setTitle] = useState('')
  const [slug, setSlug] = useState('')
  const [versionLabel, setVersionLabel] = useState('')
  const [message, setMessage] = useState('')
  const [job, setJob] = useState(null)

  async function upload(e) {
    e.preventDefault()
    if (!file) {
      setMessage('Choose a PDF or CSV file first.')
      return
    }
    const fd = new FormData()
    fd.append('file', file)
    fd.append('title', title)
    fd.append('slug', slug)
    fd.append('version_label', versionLabel)
    const data = await api('/api/resources/upload', { method: 'POST', body: fd })
    if (!data.ok) {
      setMessage(data.message || 'Upload failed.')
      return
    }
    setMessage(`Import job #${data.job_id} queued.`)
    setJob({ id: data.job_id, status: data.status })
    onImported()
  }

  useEffect(() => {
    if (!job?.id) return
    if (job.status === 'succeeded' || job.status === 'failed') return

    const timer = setInterval(async () => {
      const data = await api(`/api/import-jobs/${job.id}`)
      if (data.ok) {
        setJob(data.job)
        if (data.job.status === 'succeeded' || data.job.status === 'failed') {
          onImported()
        }
      }
    }, 1800)

    return () => clearInterval(timer)
  }, [job?.id, job?.status, onImported])

  return (
    <section className="panel">
      <h2>Import Center</h2>
      <form onSubmit={upload} className="import-form">
        <label>
          Resource file (PDF or CSV)
          <input type="file" accept=".pdf,.csv" onChange={(e) => setFile(e.target.files?.[0] || null)} required />
        </label>
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Resource title" />
        </label>
        <label>
          Slug
          <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="resource-slug" />
        </label>
        <label>
          Version label
          <input value={versionLabel} onChange={(e) => setVersionLabel(e.target.value)} placeholder="v1" />
        </label>
        <button type="submit">Upload and Start Import</button>
      </form>
      {message ? <p>{message}</p> : null}

      {job ? (
        <div className="job">
          <h3>Job #{job.id}</h3>
          <p>Status: <strong>{job.status}</strong></p>
          {job.error_summary ? <p className="error">{job.error_summary}</p> : null}
          {job.report ? (
            <pre>{JSON.stringify(job.report, null, 2)}</pre>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function DraftReviewTab({ resources }) {
  const [resourceId, setResourceId] = useState('')
  const [versions, setVersions] = useState([])
  const [versionId, setVersionId] = useState('')
  const [cards, setCards] = useState([])
  const [selectedCardId, setSelectedCardId] = useState('')
  const [message, setMessage] = useState('')

  const selectedCard = useMemo(
    () => cards.find((c) => String(c.id) === String(selectedCardId)) || null,
    [cards, selectedCardId]
  )

  useEffect(() => {
    if (!resourceId) {
      setVersions([])
      return
    }
    api(`/api/resources/${resourceId}/versions`).then((data) => {
      const all = data.ok ? data.versions : []
      setVersions(all.filter((v) => v.status === 'draft'))
      setVersionId('')
      setCards([])
      setSelectedCardId('')
    })
  }, [resourceId])

  async function loadDrafts() {
    if (!versionId) return
    const data = await api(`/api/resource-versions/${versionId}/drafts`)
    if (!data.ok) {
      setMessage(data.message || 'Failed to load drafts.')
      return
    }
    setCards(data.cards)
    if (data.cards.length > 0) setSelectedCardId(String(data.cards[0].id))
  }

  async function saveCard() {
    if (!selectedCard) return
    const payload = {
      chapter: selectedCard.chapter,
      question_raw: selectedCard.question_raw,
      answer_key: selectedCard.answer_key,
      answer_text_raw: selectedCard.answer_text_raw,
      explanation_raw: selectedCard.explanation_raw,
      options: Object.fromEntries(Object.entries(selectedCard.options).map(([k, v]) => [k, v.raw]))
    }
    const data = await api(`/api/cards/${selectedCard.id}`, {
      method: 'POST',
      body: JSON.stringify(payload)
    })
    if (!data.ok) {
      setMessage(data.message || 'Save failed.')
      return
    }
    setCards(cards.map((c) => (c.id === data.card.id ? data.card : c)))
    setMessage('Draft card updated.')
  }

  async function publishVersion() {
    if (!versionId) return
    const data = await api(`/api/resource-versions/${versionId}/publish`, { method: 'POST' })
    setMessage(data.ok ? 'Version published.' : data.message || 'Publish failed.')
  }

  function patchSelected(field, value) {
    if (!selectedCard) return
    setCards((prev) =>
      prev.map((c) => (c.id === selectedCard.id ? { ...c, [field]: value } : c))
    )
  }

  function patchOption(optKey, value) {
    if (!selectedCard) return
    setCards((prev) =>
      prev.map((c) => {
        if (c.id !== selectedCard.id) return c
        return {
          ...c,
          options: {
            ...c.options,
            [optKey]: { ...c.options[optKey], raw: value }
          }
        }
      })
    )
  }

  return (
    <section className="panel">
      <h2>Draft Review Editor</h2>
      <div className="toolbar">
        <select value={resourceId} onChange={(e) => setResourceId(e.target.value)}>
          <option value="">Select resource</option>
          {resources.map((r) => (
            <option key={r.id} value={r.id}>{r.title}</option>
          ))}
        </select>
        <select value={versionId} onChange={(e) => setVersionId(e.target.value)}>
          <option value="">Select draft version</option>
          {versions.map((v) => (
            <option key={v.id} value={v.id}>{v.version_label}</option>
          ))}
        </select>
        <button onClick={loadDrafts}>Load Drafts</button>
        <button onClick={publishVersion} disabled={!versionId}>Publish Version</button>
      </div>

      <div className="two-col">
        <div>
          <ul className="list">
            {cards.map((c) => (
              <li key={c.id}>
                <button className="list-item" onClick={() => setSelectedCardId(String(c.id))}>
                  <strong>Q{c.question_number}</strong>
                  <span>{c.question_norm.slice(0, 80)}...</span>
                </button>
              </li>
            ))}
            {cards.length === 0 ? <li>No draft cards loaded.</li> : null}
          </ul>
        </div>
        <div>
          {selectedCard ? (
            <div className="editor">
              <label>
                Chapter
                <input value={selectedCard.chapter} onChange={(e) => patchSelected('chapter', e.target.value)} />
              </label>
              <label>
                Question
                <textarea value={selectedCard.question_raw} onChange={(e) => patchSelected('question_raw', e.target.value)} />
              </label>
              <div className="options">
                {Object.entries(selectedCard.options).map(([k, v]) => (
                  <label key={k}>
                    Option {k}
                    <textarea value={v.raw} onChange={(e) => patchOption(k, e.target.value)} />
                  </label>
                ))}
              </div>
              <label>
                Answer Key
                <input value={selectedCard.answer_key} onChange={(e) => patchSelected('answer_key', e.target.value.toUpperCase())} maxLength={1} />
              </label>
              <label>
                Answer Text
                <textarea value={selectedCard.answer_text_raw} onChange={(e) => patchSelected('answer_text_raw', e.target.value)} />
              </label>
              <label>
                Explanation
                <textarea value={selectedCard.explanation_raw} onChange={(e) => patchSelected('explanation_raw', e.target.value)} />
              </label>
              <button onClick={saveCard}>Save Draft Card</button>
              {selectedCard.warnings?.length ? (
                <details>
                  <summary>Parse Warnings ({selectedCard.warnings.length})</summary>
                  <ul>
                    {selectedCard.warnings.map((w, idx) => (
                      <li key={`${w.code}-${idx}`}>{w.code}: {w.detail}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ) : (
            <p>Select a card to edit.</p>
          )}
        </div>
      </div>
      {message ? <p>{message}</p> : null}
    </section>
  )
}

function StudyTab({ resources }) {
  const [resourceId, setResourceId] = useState('')
  const [chapter, setChapter] = useState('')
  const [card, setCard] = useState(null)
  const [reveal, setReveal] = useState(false)
  const [message, setMessage] = useState('')

  async function loadNext() {
    const q = new URLSearchParams()
    if (resourceId) q.set('resource_id', resourceId)
    if (chapter) q.set('chapter', chapter)
    const data = await api(`/api/study/next?${q.toString()}`)
    if (!data.ok) {
      setMessage(data.message || 'No cards available.')
      setCard(null)
      return
    }
    setCard(data.card)
    setReveal(false)
    setMessage('')
  }

  async function grade(result) {
    if (!card) return
    await api('/api/study/grade', {
      method: 'POST',
      body: JSON.stringify({ card_id: card.id, result })
    })
    loadNext()
  }

  async function archiveCard() {
    if (!card) return
    await api(`/api/cards/${card.id}/archive`, { method: 'POST' })
    loadNext()
  }

  useEffect(() => {
    function onKey(e) {
      if (!card) return
      if (e.key === 'r') setReveal((x) => !x)
      if (e.key === 'n') loadNext()
      if (e.key === 'c' && reveal) grade('correct')
      if (e.key === 'i' && reveal) grade('incorrect')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [card, reveal])

  return (
    <section className="panel">
      <h2>Study Deck</h2>
      <div className="toolbar">
        <select value={resourceId} onChange={(e) => setResourceId(e.target.value)}>
          <option value="">All resources</option>
          {resources.map((r) => (
            <option key={r.id} value={r.id}>{r.title}</option>
          ))}
        </select>
        <input value={chapter} onChange={(e) => setChapter(e.target.value)} placeholder="Chapter filter (exact)" />
        <button onClick={loadNext}>Next Card (N)</button>
      </div>

      {card ? (
        <div className="study-card">
          <header>
            <span>{card.resource_title}</span>
            <span>{card.chapter}</span>
            <span>Q{card.question_number}</span>
          </header>
          <h3>{card.question_raw}</h3>
          <ul>
            {Object.entries(card.options).map(([k, v]) => (
              <li key={k}><strong>{k}.</strong> {v.raw}</li>
            ))}
          </ul>
          <div className="actions">
            <button onClick={() => setReveal((x) => !x)}>Reveal (R)</button>
            <button onClick={archiveCard}>Archive</button>
          </div>

          {reveal ? (
            <div className="answer-box">
              <p><strong>Answer:</strong> {card.answer_key} · {card.answer_text_raw}</p>
              <p>{card.explanation_raw}</p>
              <div className="actions">
                <button onClick={() => grade('correct')}>Correct (C)</button>
                <button onClick={() => grade('incorrect')}>Incorrect (I)</button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p>{message || 'Press Next Card to start.'}</p>
      )}
      <p className="hint">Keyboard: R reveal, C correct, I incorrect, N next.</p>
    </section>
  )
}

function ProgressTab() {
  const [data, setData] = useState(null)

  async function load() {
    const result = await api('/api/study/progress')
    setData(result.ok ? result : null)
  }

  useEffect(() => {
    load()
  }, [])

  if (!data) {
    return (
      <section className="panel">
        <h2>Progress</h2>
        <p>Loading...</p>
      </section>
    )
  }

  const summary = data.summary
  return (
    <section className="panel">
      <h2>Progress Dashboard</h2>
      <div className="metrics">
        <article><strong>{summary.total_published_cards}</strong><span>Published Cards</span></article>
        <article><strong>{summary.times_seen}</strong><span>Total Attempts</span></article>
        <article><strong>{summary.unique_seen_cards}</strong><span>Unique Cards Seen</span></article>
        <article><strong>{(summary.accuracy * 100).toFixed(1)}%</strong><span>Accuracy</span></article>
      </div>

      <h3>By Resource</h3>
      <ul className="list">
        {data.by_resource.map((r) => (
          <li key={r.resource_id} className="card-mini">
            <strong>{r.resource_title}</strong>
            <span>{r.unique_seen_cards}/{r.total_cards} seen</span>
            <span>{r.times_correct} correct · {r.times_incorrect} incorrect</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

export default function App() {
  const [me, setMe] = useState({ authenticated: false, username: '' })
  const [activeTab, setActiveTab] = useState('Study')
  const [resources, setResources] = useState([])

  async function loadMe() {
    const data = await api('/api/me')
    if (data.ok) setMe(data)
  }

  async function loadResources() {
    const data = await api('/api/resources')
    if (data.ok) setResources(data.resources)
  }

  useEffect(() => {
    loadMe()
  }, [])

  useEffect(() => {
    if (me.authenticated) loadResources()
  }, [me.authenticated])

  async function logout() {
    await api('/api/logout', { method: 'POST' })
    setMe({ authenticated: false, username: '' })
  }

  if (!me.authenticated) {
    return (
      <main className="app-shell">
        <AuthPanel onAuth={loadMe} />
      </main>
    )
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <h1>Flashcard Studio</h1>
        <div>
          <span>{me.username}</span>
          <button onClick={logout}>Log out</button>
        </div>
      </header>

      <nav className="tabbar">
        {TABS.map((tab) => (
          <button
            key={tab}
            className={tab === activeTab ? 'active' : ''}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </nav>

      {activeTab === 'Study' ? <StudyTab resources={resources} /> : null}
      {activeTab === 'Resources' ? <ResourcesTab resources={resources} /> : null}
      {activeTab === 'Import' ? <ImportTab onImported={loadResources} /> : null}
      {activeTab === 'Draft Review' ? <DraftReviewTab resources={resources} /> : null}
      {activeTab === 'Progress' ? <ProgressTab /> : null}
    </main>
  )
}
