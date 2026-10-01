import { useEffect, useState } from 'react'
import { api } from '../api'
import type { PublikStatus } from '../types'
import ChatGPTCard from './ChatGPTCard'
import {
  LinkGateCard,
  PUBLIK_DATA_PATH,
  PUBLIK_PRE_SETUP,
  PublikReady,
  linkGate
} from './PublikCard'

/**
 * Three beats: what this is → pick the brain (publik API preselected, your
 * own Gemini key, or local Ollama) → go. The optional Instagram feedback
 * module gets its own guided flow later (Settings → Connect Instagram), so
 * first-run stays under a minute.
 *
 * "Continue with publik API" is the consent moment: nothing is posted to
 * publik before that tap.
 *
 * Each provider has its own readiness check. ChatGPT needs no Publik account.
 */

interface Props {
  onDone: () => void
}

export default function Onboarding({ onDone }: Props) {
  const [step, setStep] = useState(0)
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  const [ollama, setOllama] = useState<{ running: boolean; models: string[] } | null>(null)
  const [brain, setBrain] = useState<'chatgpt' | 'publik' | 'gemini' | 'ollama'>('chatgpt')
  const [chatgptReady, setChatgptReady] = useState(false)
  const [publik, setPublik] = useState<PublikStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  useEffect(() => {
    api.checkOllama().then(setOllama).catch(() => setOllama({ running: false, models: [] }))
    api.setupState().then((s) => setSaved(s.has_gemini_key)).catch(() => {})
    // A reinstall on a computer that already has a key skips the mint; ask
    // the server whether it is linked (no key = no network call).
    api.publikStatus().then((p) => p.provisioned && setPublik(p)).catch(() => null)
  }, [])

  useEffect(() => {
    if (brain === 'publik') api.publikRefresh().then(setPublik).catch(() => {})
  }, [brain])

  async function connectPublik() {
    setBusy(true)
    setNote(null)
    try {
      setPublik(await api.publikProvision())
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  const linked = linkGate(publik) === 'linked'
  const ready = brain === 'chatgpt' ? chatgptReady : brain === 'publik' ? linked
    : brain === 'gemini' ? saved : !!(ollama?.running && ollama.models.length)

  async function saveKey() {
    if (!key.trim()) return
    await api.saveGeminiKey(key)
    setSaved(true)
  }

  return (
    <div className="onboarding">
      <div className="grain" />
      {step === 0 && (
        <section className="ob-step" key="s0">
          <p className="ob-kicker">publikclip</p>
          <h1 className="ob-title">
            THE CLIPPER
            <br />
            THAT SHOWS
            <br />
            ITS WORK<span className="amber">.</span>
          </h1>
          <p className="ob-body">
            Long video in, vertical clips out. Speech, laughter, speakers, and camera
            moves are all computed <em>on this machine</em>. Transcript excerpts and
            sampled frames go to the scoring provider you choose — and
            every score comes with the full audit trail of how it was made.
          </p>
          <button className="btn-primary" onClick={() => setStep(1)}>
            Set it up
          </button>
        </section>
      )}
      {step === 1 && (
        <section className="ob-step" key="s1">
          <p className="ob-kicker">01 / the scoring brain</p>
          <h2 className="ob-h2">Pick how moments get judged</h2>
          <div className="ob-cards">
            <div className={`ob-card ${brain === 'chatgpt' ? '' : 'dim'}`} onClick={() => setBrain('chatgpt')}>
              <ChatGPTCard onChange={(s) => setChatgptReady(s.connected)} />
            </div>
            <div
              className={`ob-card ${brain === 'publik' ? '' : 'dim'} ${linked ? 'done' : ''}`}
              onClick={() => setBrain('publik')}
            >
              <h3>publik API</h3>
              {publik?.provisioned ? (
                <>
                  <button className="btn-secondary" disabled>publik API ready ✓</button>
                  {linked ? (
                    <PublikReady publik={publik} />
                  ) : (
                    <LinkGateCard publik={publik} inline onChange={setPublik} />
                  )}
                </>
              ) : (
                <>
                  <p>{PUBLIK_PRE_SETUP}</p>
                  <p>{PUBLIK_DATA_PATH}</p>
                  <div className="ob-key-row">
                    <button className="btn-secondary" onClick={connectPublik} disabled={busy}>
                      {busy ? 'Setting up…' : 'Continue with publik API'}
                    </button>
                  </div>
                </>
              )}
              {note && <p className="ig-message mono">{note}</p>}
            </div>
            <div
              className={`ob-card ${brain === 'gemini' ? '' : 'dim'} ${saved ? 'done' : ''}`}
              onClick={() => setBrain('gemini')}
            >
              <h3>Your own Gemini key</h3>
              <p>
                Prefer your own Google key? Paste it here (aistudio.google.com);
                publikclip then talks to Google directly.
              </p>
              <div className="ob-key-row">
                <input
                  type="password"
                  placeholder="AIza…"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && saveKey()}
                />
                <button className="btn-secondary" onClick={saveKey} disabled={!key.trim()}>
                  {saved ? 'Saved ✓' : 'Save'}
                </button>
              </div>
            </div>
            <div
              className={`ob-card ${ollama?.running && brain === 'ollama' ? '' : 'dim'}`}
              onClick={() => setBrain('ollama')}
            >
              <h3>
                Ollama <span className={`led ${ollama?.running ? 'led-on' : 'led-off'}`} />
              </h3>
              <p>
                {ollama === null
                  ? 'Checking…'
                  : ollama.running
                    ? `Running locally (${ollama.models.filter((m) => !m.includes('embed')).slice(0, 2).join(', ') || 'no chat models'}). Zero cost, fully offline — scores are labeled "local estimate" because small models judge humor less reliably.`
                    : 'Not detected. Install ollama.com and pull a model (e.g. llama3.1:8b) to run fully offline.'}
              </p>
            </div>
          </div>
          <p className="ob-fine">
            You can switch per-run. Everything else — transcription, laughter
            detection, speaker tracking, rendering — is local either way.
          </p>
          {!ready && <p className="ob-fine">Connect the selected scoring provider to continue.</p>}
          <button className="btn-primary" onClick={() => { localStorage.setItem('scoring-provider', brain); setStep(2) }} disabled={!ready}>
            Continue
          </button>
        </section>
      )}
      {step === 2 && (
        <section className="ob-step" key="s2">
          <p className="ob-kicker">02 / one honest warning</p>
          <h2 className="ob-h2">First run downloads the models</h2>
          <p className="ob-body">
            About <span className="mono">4–5 GB</span> of open speech and audio models,
            fetched once into <span className="mono">~/.publikclip</span>. An hour-long
            podcast then takes a while on-device — the progress bar never lies to you,
            and every stage checkpoints, so you can quit and resume anytime.
          </p>
          <button className="btn-primary" onClick={onDone}>
            Open the studio
          </button>
        </section>
      )}
    </div>
  )
}
