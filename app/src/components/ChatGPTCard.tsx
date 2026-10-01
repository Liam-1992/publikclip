import { useEffect, useState } from 'react'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import type { ChatGPTModel, ChatGPTStatus } from '../types'

export default function ChatGPTCard({ onChange }: { onChange?: (s: ChatGPTStatus) => void }) {
  const [status, setStatus] = useState<ChatGPTStatus | null>(null)
  const [models, setModels] = useState<ChatGPTModel[]>([])
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [welcome, setWelcome] = useState(false)

  async function accept(s: ChatGPTStatus) {
    setStatus(s)
    onChange?.(s)
    setModels([])
    if (s.connected) {
      const catalog = await api.chatgptModels()
      setModels(catalog.models)
      if (!catalog.models.length) setNote('No models are currently available for this account.')
    }
  }

  useEffect(() => {
    let live = true
    api.chatgptStatus().then((s) => {
      if (live) accept(s).catch((e) => live && setNote(String(e)))
    }).catch((e) => live && setNote(String(e)))
    return () => { live = false }
  }, [])

  async function act(fn: () => Promise<ChatGPTStatus>, login = false) {
    setBusy(true)
    setNote(login ? 'Finish signing in and granting plan access in your browser. This expires in five minutes.' : null)
    try {
      const s = await fn()
      setNote(s.revocation_confirmed === false
        ? 'Signed out locally. Remote revocation was not confirmed; disconnect this app in ChatGPT Settings.' : null)
      if (login && !localStorage.getItem('chatgpt-plan-welcome')) setWelcome(true)
      await accept(s)
    } catch (e) {
      setNote(String(e))
    } finally {
      setBusy(false)
    }
  }

  return <div className="chatgpt-card">
    <h3>ChatGPT plan <span className="chip chip-amber">Plus / eligible plans</span></h3>
    <p className="ig-intro">Use eligible AI requests from your ChatGPT plan or credits balance for clip scoring, frame analysis, and music suggestions. Choose an image-capable model for frame analysis. Plan limits apply.</p>
    {status?.connected && <p className="ig-message">Using ChatGPT plan · {status.accounts.find((a) => a.id === status.active)?.label}</p>}
    {!!status?.accounts.length && <div className="ig-form">
      <select aria-label="ChatGPT account" value={status.active || ''} disabled={busy}
        onChange={(e) => {
          const id = e.target.value
          const connected = status.accounts.find((a) => a.id === id)?.connected
          act(() => connected ? api.chatgptSelectAccount(id) : api.chatgptLogin(id), !connected)
        }}>
        {status.accounts.map((a) => <option key={a.id} value={a.id}>{a.label}{a.connected ? '' : ' (signed out)'}</option>)}
      </select>
    </div>}
    {status?.connected && <div className="ig-form">
      <select aria-label="ChatGPT model" disabled={busy || !models.length}
        value={status.model || models[0]?.slug || ''}
        onChange={(e) => act(() => api.chatgptSelectModel(e.target.value))}>
        {status.model && !models.some((m) => m.slug === status.model) && <option value={status.model}>Unavailable: {status.model}</option>}
        {models.map((m) => <option key={m.slug} value={m.slug}>{m.display_name}</option>)}
      </select>
    </div>}
    <div className="publik-actions">
      <button className="btn-secondary" disabled={busy} onClick={() => act(() => api.chatgptLogin(status?.active || undefined), true)}>
        {busy ? 'Please wait…' : status?.connected ? 'Reauthorize ChatGPT' : 'Continue with ChatGPT'}
      </button>
      {!!status?.accounts.length && <button className="btn-ghost" disabled={busy} onClick={() => act(() => api.chatgptLogin(), true)}>Add account</button>}
      {status?.connected && <>
        <button className="btn-ghost" disabled={busy} onClick={() => act(api.chatgptLogout)}>Sign out</button>
        <button className="btn-ghost" disabled={busy} onClick={() => act(async () => { await api.chatgptModels(); return api.chatgptStatus() })}>Refresh models</button>
      </>}
      <button className="btn-ghost" onClick={() => openUrl('https://chatgpt.com/#settings/Usage')}>Manage usage ↗</button>
    </div>
    {welcome && <div role="dialog" aria-label="ChatGPT plan usage" className="ig-message">
      <strong>You’re using your ChatGPT plan.</strong>
      <p>Eligible AI requests in this app use your ChatGPT plan. Manage usage in your ChatGPT settings.</p>
      <button className="btn-secondary" onClick={() => { localStorage.setItem('chatgpt-plan-welcome', '1'); setWelcome(false) }}>Got it</button>
    </div>}
    {note && <p className="ig-message mono" role="status">{note}</p>}
  </div>
}
