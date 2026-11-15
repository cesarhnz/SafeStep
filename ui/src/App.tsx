import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { Activity, ArrowDown, ArrowRight, ArrowUpRight, Bell, Check, CheckCheck, ChevronRight, CircleCheck, CircleHelp, Clock3, Database, ExternalLink, Footprints, Heart, Home, Info, Leaf, LockKeyhole, Mail, MapPin, Menu, Play, Radio, RotateCcw, ShieldCheck, Sparkles, Sprout, Sun, TriangleAlert, WifiOff, X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { EMPTY_DATA, eventTitle, isOpenFall, notificationLabel, parseDashboard, sampleEvents, timeLabel } from '@/lib/events'
import type { CareEvent, DashboardData } from '@/lib/events'

function Brand({ small = false }: { small?: boolean }) {
  return <span className={`brand ${small ? 'brand-small' : ''}`}><span className="brand-symbol"><Leaf strokeWidth={1.8} /></span><span>SafeStep<span className="brand-period">.</span></span></span>
}

function Garden({ className = '' }: { className?: string }) {
  return <svg className={className} viewBox="0 0 400 300" fill="none" aria-hidden="true">
    <circle cx="218" cy="143" r="112" fill="#e8ecdd" /><circle cx="313" cy="66" r="26" fill="#efcfb3" />
    <path d="M51 249c60-22 119-21 184-8 48 10 89 8 128-2" stroke="#c8d1b9" strokeWidth="2" />
    <path d="M118 242c3-50 6-87 23-129m-16 77c-35 0-55-23-56-47 27-2 52 19 56 47Zm8-32c-4-29 7-49 32-60 10 24-5 46-32 60Zm-10 62c-28-2-45-13-48-34 26-7 46 10 48 34Zm10-29c30 1 49-17 52-39-25-1-45 12-52 39Z" fill="#8da68a" />
    <path d="M282 241c-3-36-1-73-7-109m6 58c24-7 36-24 33-44-25 2-36 20-33 44Zm-3-25c-23-1-37-17-39-36 23-1 36 14 39 36Zm3 55c-25-2-38-16-37-34 25-1 34 13 37 34Z" fill="#a6b399" />
    <path d="M163 213c0-31 25-57 57-57s57 26 57 57v28H163v-28Z" fill="#f9f8f0" stroke="#c2cfb9" strokeWidth="2" />
    <path d="M175 213c0-25 20-45 45-45s45 20 45 45v28h-90v-28Z" stroke="#d8dfce" strokeWidth="2" />
    <path d="M209 241v-35h23v35" fill="#d4dec7" /><path d="M198 196h-10m54 0h10" stroke="#a9b99d" strokeWidth="3" strokeLinecap="round" />
    <path d="M194 119c-9-10-23 3-13 14l14 13 15-14c11-12-5-23-16-13Z" fill="#b3c6a3" />
    <path d="m61 92 5-8m-5 8 9 2m-9-2-4-7" stroke="#b7c5a6" strokeWidth="2" strokeLinecap="round" />
    <circle cx="345" cy="194" r="3" fill="#b7c5a6" /><circle cx="93" cy="62" r="3" fill="#d9bea2" />
  </svg>
}

function useDashboard() {
  const [data, setData] = useState<DashboardData>(EMPTY_DATA)
  const [loading, setLoading] = useState(true)
  const [stale, setStale] = useState(false)
  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    let controller: AbortController
    const poll = async () => {
      controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), 7500)
      try {
        const response = await fetch('/api/dashboard', { signal: controller.signal, cache: 'no-store' })
        if (!response.ok) throw new Error('Unable to connect')
        const result = parseDashboard(await response.json())
        if (alive) { setData(result); setStale(false) }
      } catch {
        if (alive) { setStale(true); setData(old => ({ ...old, connected: false, monitoring: 'unknown' })) }
      } finally {
        clearTimeout(timeout)
        if (alive) { setLoading(false); timer = setTimeout(poll, 6000) }
      }
    }
    void poll()
    return () => { alive = false; clearTimeout(timer); controller?.abort() }
  }, [])
  return { data, loading, stale }
}

function App() {
  const { data, loading, stale } = useDashboard()
  const [menu, setMenu] = useState(false)
  const [profile, setProfile] = useState(false)
  const [step, setStep] = useState(-1)
  const [demoEvent, setDemoEvent] = useState<CareEvent | null>(null)
  const [examples] = useState(() => sampleEvents())
  const [selected, setSelected] = useState<CareEvent | null>(null)
  const [filter, setFilter] = useState('all')
  const runId = useRef(0)
  const location = useLocation()
  const hasRealEvents = data.events.some(event => event.source === 'real')
  const events = [...(demoEvent ? [demoEvent] : []), ...data.events, ...(data.events.length === 0 ? examples : [])].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
  const realFall = data.events.find(isOpenFall)
  const demoRunning = step >= 0 && step < 3

  useEffect(() => { setMenu(false); window.scrollTo(0, 0); document.title = `${location.pathname === '/about' ? 'About SafeStep' : 'Home'} — SafeStep` }, [location.pathname])
  useEffect(() => () => { runId.current++ }, [])
  const reset = () => { runId.current++; setStep(-1); setDemoEvent(null) }
  const simulate = async () => {
    const id = ++runId.current
    const event: CareEvent = { id: `demo-${Date.now()}`, type: 'fall_detected', status: 'alerted', room: 'Living room', timestamp: new Date().toISOString(), source: 'demo', local_demo: true, notification: 'pending', details: 'A simulated fall for exploring SafeStep. No real alert was sent and no data was written to Databricks.' }
    setDemoEvent(event); setStep(0)
    for (let next = 1; next <= 3; next++) {
      await new Promise(resolve => setTimeout(resolve, 1350))
      if (id !== runId.current) return
      setStep(next)
      if (next === 2) setDemoEvent(old => old ? { ...old, notification: 'simulated' } : old)
    }
  }

  return <div className="app-shell">
    <a href="#main" className="skip-link">Skip to content</a>
    {menu && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMenu(false)} />}
    <aside className={`sidebar ${menu ? 'sidebar-open' : ''}`}><button className="mobile-nav-close" aria-label="Close menu" onClick={() => setMenu(false)}><X size={19} /></button>
      <Link to="/" aria-label="SafeStep home" className="logo-link"><Brand /></Link>
      <div className="sidebar-section-label">YOUR CARE SPACE</div>
      <nav aria-label="Main navigation">
        <NavLink to="/" end><Home size={19} /><span>Home</span></NavLink>
        <NavLink to="/about"><Heart size={19} /><span>About SafeStep</span></NavLink>
      </nav>
      <div className="sidebar-bottom">
        <div className="privacy-note"><span className="privacy-icon"><ShieldCheck size={22} strokeWidth={1.6} /></span><strong>Care with confidence.</strong><p>Your privacy is always<br />part of the picture.</p><Link to="/about#privacy">Our privacy promise <ArrowUpRight size={13} /></Link></div>
        <button className="care-profile" onClick={() => setProfile(true)}><span className="avatar"><Heart size={18} /></span><span><strong>Your care circle</strong><small>A little closer, always</small></span><ChevronRight size={15} /></button>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="topbar-title"><Button variant="ghost" size="icon" className="mobile-menu" aria-label="Open navigation" aria-expanded={menu} onClick={() => setMenu(!menu)}>{menu ? <X /> : <Menu />}</Button><span>{location.pathname === '/about' ? 'The heart behind SafeStep' : 'Your daily peace of mind'}</span></div>
        <div className="topbar-right"><span className="today">{new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date())}</span><span className="topbar-divider" /><button className="notification-button" aria-label="View recent activity" onClick={() => { if (location.pathname !== '/') { window.location.href = '/#activity' } else document.getElementById('activity')?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }}><Bell size={19} />{realFall && <span className="notification-dot" />}</button><button className="avatar avatar-small" aria-label="Open your care circle" onClick={() => setProfile(true)}>C</button></div>
      </header>
      <main id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<div className="dashboard page-enter">
            <section className="page-heading"><div><div className="eyebrow"><Sun size={15} /> A LITTLE REASSURANCE, EVERY DAY</div><h1>Peace of mind,<br /><em>every step of the way.</em></h1><p>A caring connection, even when you can’t be there.</p></div><div className={`connection-chip ${data.connected ? 'is-connected' : ''}`}><span className="status-dot" />{loading ? 'Connecting…' : data.connected ? data.dry_run ? 'Service in test mode' : 'Connected to your home' : 'Explore in demo mode'}<CircleHelp size={14} aria-hidden="true" /></div></section>
            {(stale && data.checked_at) && <div className="inline-warning" role="status"><WifiOff size={16} /> Connection lost. Showing the last available records; monitoring status is unavailable.</div>}
            <div className="overview-grid">
              <section className={`status-card ${realFall || step >= 0 ? 'status-alert' : ''}`} aria-labelledby="monitoring-title" aria-live="polite">
                <div className="card-kicker"><span className={`status-dot ${realFall || step >= 0 ? 'dot-peach' : ''}`} /> MONITORING STATUS<span className="badge">{realFall ? data.connected ? 'Live event' : 'Recorded event' : step >= 0 || !data.connected ? 'Demo' : 'Live'}</span></div>
                <div className="status-body"><div><h2 id="monitoring-title">{realFall || step >= 0 ? realFall?.status === 'recovered' ? 'Recovery detected' : 'Fall detected' : data.connected ? data.monitoring === 'active' ? 'All clear' : data.monitoring === 'offline' ? 'Let’s reconnect' : 'Waiting to connect' : 'All clear'}</h2><p>{realFall ? realFall.status === 'recovered' ? `A fall was recorded in the ${realFall.room.toLowerCase()}. Recovery was detected; check in to make sure they’re okay.` : `An event in the ${realFall.room.toLowerCase()} needs your attention.` : step >= 0 ? 'This is a simulation. See how care comes together.' : data.connected ? data.monitoring === 'active' ? 'No active fall alerts. A little more room to breathe.' : data.monitoring === 'offline' ? 'Monitoring is interrupted. Please check your local detector.' : 'Your service is ready. Waiting for the local detector.' : 'A little peace of mind looks like this.'}</p><div className="status-footnote">{realFall || step >= 0 ? <TriangleAlert size={14} /> : <ShieldCheck size={14} />}{realFall ? realFall.status === 'recovered' ? 'Recovery detected · Please check in' : 'Check in with your loved one' : step >= 0 ? 'Demo only · No messages are sent' : data.connected ? data.monitoring === 'active' ? 'Local monitoring is active' : 'Live monitoring is not confirmed' : 'Demo preview · Live monitoring is not connected'}</div></div><div className="status-orbit"><div className="orbit-ring orbit-ring-outer" /><div className="orbit-ring orbit-ring-inner" /><div className="shield-center">{realFall || step >= 0 ? <TriangleAlert size={42} strokeWidth={1.35} /> : <ShieldCheck size={46} strokeWidth={1.3} />}</div><span className="orbit-spark spark-one" /><span className="orbit-spark spark-two" /></div></div>
                <div className="status-bottom"><span><LockKeyhole size={13} /> Private by design. Always.</span><span>{realFall ? 'Attention needed' : step >= 0 ? 'Simulated event' : data.connected ? data.monitoring === 'active' ? 'Looking out for you' : 'Awaiting monitoring' : 'An example of everyday reassurance'}<Heart size={13} /></span></div>
              </section>
              <section className="demo-card card" aria-labelledby="demo-title"><div className="demo-card-top"><span className="icon-tile peach-tile"><Sparkles size={20} strokeWidth={1.6} /></span><span className="badge badge-peach">INTERACTIVE DEMO</span></div><h2 id="demo-title">See care in action.</h2><p>From a detected fall to a helping hand. <br />Try the SafeStep experience.</p><Button onClick={() => void simulate()} disabled={demoRunning || step === 3}><Play size={15} fill="currentColor" />{demoRunning ? 'Simulation in progress…' : step === 3 ? 'Demo complete' : 'Simulate Fall'}<ArrowRight size={16} /></Button><span className="demo-reassurance">No real alerts. Just a little reassurance.</span></section>
            </div>
            <section className={`workflow ${step >= 0 ? 'workflow-active' : ''}`} aria-label="Detection to response workflow" aria-live="polite"><div className="workflow-intro"><span className="eyebrow">A THOUGHTFUL RESPONSE</span><small>{step === 3 ? 'One connected circle of care.' : 'Every moment has a next step.'}</small></div><div className="workflow-steps">{[{ label: 'Fall detected', icon: Activity }, { label: 'Event recorded', icon: CheckCheck }, { label: 'Caregiver notified', icon: Bell }, { label: 'Databricks updated', icon: Database }].map(({ label, icon: Icon }, index) => <div className={`workflow-step ${step >= index ? 'step-complete' : ''}`} key={label}><span className="step-icon">{step >= index ? <Check size={15} /> : <Icon size={15} />}</span><span>{label}{step >= index && <small>Simulated</small>}</span>{index < 3 && <ChevronRight className="step-chevron" size={14} />}</div>)}</div>{step >= 0 && <Button variant="ghost" size="sm" onClick={reset} className="reset-button"><RotateCcw size={14} />Reset</Button>}</section>
            <div className="section-heading"><div><h2>Connected when it counts<span className="heading-dot">.</span></h2><p>A thoughtful heads-up, wherever you are.</p></div><span className="preview-tag"><span className="status-dot" />{demoEvent ? 'SIMULATION PREVIEWS' : realFall ? 'ALERT PREVIEWS' : 'A PREVIEW OF YOUR ALERTS'}</span></div>
            <NotificationPreviews event={realFall || demoEvent} demoStep={step} />
            <div className="lower-grid">
              <section className="activity-card card" id="activity" aria-labelledby="activity-title"><div className="activity-heading"><div><h2 id="activity-title">Recent activity</h2><p>Newest falls first, by detection time.</p></div><span className="icon-tile small-tile"><Clock3 size={18} /></span></div><div className="activity-filters" aria-label="Filter recent activity">{[['all', 'All activity'], ['real', 'Real events'], ['demo', 'Demo events']].map(([value, label]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)} className={filter === value ? 'filter-active' : ''}>{label}</button>)}</div><div className="activity-list">{events.filter(e => filter === 'all' || e.source === filter).slice(0, 5).map(event => <button className="activity-row" key={event.id} onClick={() => setSelected(event)}><span className={`event-icon ${event.source === 'real' && isOpenFall(event) ? 'event-urgent' : ''}`}>{event.status === 'resolved' || event.status === 'recovered' ? <CircleCheck size={18} /> : <Activity size={18} />}</span><span className="event-description"><strong>{eventTitle(event)}<span className={`badge ${event.source === 'demo' ? '' : 'badge-real'}`}>{event.source === 'demo' ? 'Demo' : 'Real'}</span></strong><small>{event.room} <span>·</span> {timeLabel(event.timestamp, { month: 'short', day: 'numeric' })}, {timeLabel(event.timestamp)}</small></span><span className={`event-notification ${event.notification === 'failed' ? 'text-alert' : ''}`}>{event.source === 'demo' ? <CheckCheck size={14} /> : event.notification === 'accepted' ? <Check size={14} /> : <Clock3 size={14} />}<span>{notificationLabel(event)}</span></span><ChevronRight size={15} className="row-chevron" /></button>)}{events.filter(e => filter === 'all' || e.source === filter).length === 0 && <div className="empty-activity"><Sprout size={26} /><strong>No {filter === 'real' ? 'real events' : 'events'} to show.</strong><p>{filter === 'real' ? 'Real event records will appear here when available.' : 'Try a simulation to see how SafeStep responds.'}</p></div>}</div><div className="activity-footer"><ShieldCheck size={13} />{hasRealEvents ? 'Real records stay separate from your demo.' : 'Demo events are labeled. Your real story stays yours.'}</div></section>
              <Insights data={data} />
            </div>
            <footer className="page-footer"><Brand small /><span>Thoughtfully built for the people who care.</span><Link to="/about">Get to know SafeStep <ArrowUpRight size={13} /></Link></footer>
          </div>} />
          <Route path="/about" element={<About />} />
          <Route path="*" element={<div className="not-found"><Sprout size={40} /><h1>Let’s get you home.</h1><p>Your care space is just one step away.</p><Button asChild><Link to="/">Go to Home <ArrowRight size={16} /></Link></Button></div>} />
        </Routes>
      </main>
    </div>
    <Dialog open={profile} onOpenChange={setProfile}><DialogContent><span className="icon-tile"><Heart size={23} /></span><DialogTitle>Your care circle</DialogTitle><DialogDescription>A caring connection starts at home. SafeStep brings local monitoring and your caregiver alerts together here.</DialogDescription><div className="detail-grid"><span>Care space</span><strong>{data.home_name}</strong><span>Alert service</span><strong>{data.connected ? data.dry_run ? 'Connected · test mode' : 'Connected' : 'Not connected'}</strong><span>Local monitoring</span><strong>{data.connected && data.monitoring === 'active' ? 'Active' : 'Not confirmed'}</strong><span>Notifications</span><strong>Configured in your local alert service</strong></div><p className="dialog-note"><LockKeyhole size={15} />Contact details and credentials stay in your local configuration.</p><Button asChild variant="outline"><Link to="/about" onClick={() => setProfile(false)}>Meet SafeStep <ArrowRight size={16} /></Link></Button></DialogContent></Dialog>
    <Dialog open={!!selected} onOpenChange={open => { if (!open) setSelected(null) }}><DialogContent>{selected && <><span className="icon-tile peach-tile"><Activity size={23} /></span><DialogTitle>{eventTitle(selected)}</DialogTitle><DialogDescription>{selected.local_demo ? 'Simulated event · No real fall or notification' : selected.source === 'demo' ? 'Demo event from your alert service · See notification status below' : 'A record from your SafeStep alert service'}</DialogDescription><div className="detail-grid"><span>Location</span><strong>{selected.room}</strong><span>Recorded</span><strong>{timeLabel(selected.timestamp, { dateStyle: 'medium', timeStyle: 'short' })}</strong><span>Event status</span><strong>{selected.status.replaceAll('_', ' ')}</strong><span>Notification</span><strong>{notificationLabel(selected)}</strong></div><p className="dialog-note">{selected.details}</p>{selected.source === 'real' && <p className="muted">“Alert service accepted” means the notification provider accepted a message. It does not confirm a caregiver received or read it.</p>}</>}</DialogContent></Dialog>
  </div>
}

function NotificationPreviews({ event, demoStep }: { event: CareEvent | null | undefined, demoStep: number }) {
  const previewTime = event ? timeLabel(event.timestamp) : '9:41 AM'
  const simulated = !event || event.source === 'demo'
  const ready = !event || event.source === 'real' || demoStep >= 2
  return <div className="notification-grid">
    <section className={`notification-card card ${event && ready ? 'notification-highlight' : ''}`}><div className="preview-card-title"><span><Bell size={16} />Caregiver alert</span><span className="preview-label">PREVIEW ONLY</span></div><div className={`phone-notification ${ready ? '' : 'preview-waiting'}`}><span className="notification-app-icon"><Leaf size={20} /></span><div className="phone-content"><div className="notification-app-name"><strong>SAFESTEP</strong><span>{event ? previewTime : 'now'}</span></div><strong>{!ready ? 'Preparing your demo alert…' : 'A little help is needed.'}</strong><p>{!ready ? 'Following the response, one thoughtful step at a time.' : `A possible fall was detected in the ${(event?.room || 'living room').toLowerCase()}. Please check in with your loved one.`}</p><span className="notification-meta"><MapPin size={11} />{event?.room || 'Living room'}<span>·</span>{previewTime}{simulated && <span className="tiny-demo">Demo</span>}</span></div></div><div className="preview-caption"><span className="caption-dot" />A timely heads-up. A familiar connection.</div></section>
    <section className={`email-card card ${event && ready ? 'notification-highlight' : ''}`}><div className="preview-card-title"><span><Mail size={16} />Caregiver email</span><span className="preview-label">PREVIEW ONLY</span></div><div className="email-preview"><div className="email-envelope"><span className="mail-avatar"><Leaf size={17} /></span><div><strong>SafeStep Care</strong><small>To: Your caregiver</small></div><span>{previewTime}</span></div><div className="email-body"><strong>A moment to check in.</strong><p>SafeStep noticed a possible fall in the {(event?.room || 'living room').toLowerCase()} at {previewTime}. A quick call or visit can make all the difference.</p><span className="email-signature">With care, <span>SafeStep</span><Heart size={11} /></span></div></div><div className="preview-caption"><Info size={12} />Email template preview. No email has been sent by this page.</div></section>
  </div>
}

function Insights({ data }: { data: DashboardData }) {
  return <section className="insights-card" aria-labelledby="insights-title"><div className="insight-top"><span className="insight-logo"><Database size={19} strokeWidth={1.5} /></span><span>THE BIGGER PICTURE</span><ArrowUpRight size={18} /></div><h2 id="insights-title">A clearer <br /><em>picture of care.</em></h2><p>Small moments can tell a bigger story. Explore fall history and patterns with Databricks, and make more informed care decisions.</p><div className="insight-mini-chart" aria-hidden="true"><span /><span /><span /><span /><span /><span /><span /><span /><span /><span /><span /><span /><svg viewBox="0 0 300 70" preserveAspectRatio="none"><path d="M0 54C25 54 24 35 49 35S77 46 98 34s25-19 48-14 28 16 49 5 25-10 42-7 38-11 63-12" stroke="currentColor" strokeWidth="2" fill="none" /></svg><span className="chart-caption">See the patterns. Support the person.</span></div>{data.dashboard_url ? <Button asChild className="insights-button"><a href={data.dashboard_url} target="_blank" rel="noopener noreferrer">View Insights in Databricks<ExternalLink size={14} /></a></Button> : <><Button className="insights-button" disabled>Databricks not configured<ExternalLink size={14} /></Button><small className="insights-config-note">Set your dashboard URL in the local configuration.</small></>}<span className="insights-note"><LockKeyhole size={11} />Only event records. Never video.</span></section>
}

const steps: { icon: LucideIcon, title: string, subtitle: string, copy: string }[] = [
  { icon: Activity, title: 'Detect', subtitle: 'A watchful eye, close to home.', copy: 'Computer vision identifies possible falls locally on the Arduino UNO Q, where the moment happens.' },
  { icon: CheckCheck, title: 'Record', subtitle: 'Just the details that matter.', copy: 'Essential event information, like when and where a fall happened, is captured securely.' },
  { icon: Bell, title: 'Notify', subtitle: 'A connection when it counts.', copy: 'Caregivers receive immediate alerts so they can check in, respond, and be there when needed.' },
  { icon: Database, title: 'Understand', subtitle: 'A clearer picture, over time.', copy: 'Databricks brings historical event records together to help caregivers recognize patterns and plan care.' },
]

function About() {
  const location = useLocation()
  useEffect(() => { if (location.hash === '#privacy') requestAnimationFrame(() => document.getElementById('privacy')?.scrollIntoView({ behavior: 'smooth' })) }, [location])
  return <div className="about-page page-enter">
    <section className="about-hero"><div><div className="eyebrow"><Sprout size={16} /> TECHNOLOGY WITH A HUMAN HEART</div><h1>Because every<br /><em>step matters.</em></h1><p>Independence for the people you love.<br />Reassurance for the people who care.</p><a className="about-scroll-link" href="#mission">Get to know SafeStep <ArrowDown size={15} /></a></div><div className="garden-wrap"><Garden className="garden-illustration" /><span className="garden-caption"><Heart size={13} />Made for more moments together.</span></div></section>
    <section className="mission-section" id="mission"><div className="eyebrow">OUR MISSION</div><div><h2>More independence. <br />A little less worry.</h2><p>Being there for someone doesn’t always mean being in the same room. SafeStep helps caregivers respond quickly to falls, while supporting the independence and dignity of the people they love.</p><p>We believe thoughtful technology should make life feel a little more human. A little more connected. And a lot more reassuring.</p></div><div className="mission-mark"><Footprints size={36} strokeWidth={1.25} /><span>Every step.<br />Every person.<br />Every day.</span></div></section>
    <section className="how-section"><div className="section-heading"><div><div className="eyebrow">SIMPLE BY DESIGN</div><h2>A thoughtful circle of care.</h2><p>Four small steps. One meaningful connection.</p></div><span className="handwritten">From awareness to understanding <ArrowDown size={16} /></span></div><div className="how-grid">{steps.map(({ icon: Icon, title, subtitle, copy }, index) => <article className="how-card" key={title}><div className="how-icon-row"><span className={`icon-tile ${index === 2 ? 'peach-tile' : ''}`}><Icon size={25} strokeWidth={1.5} /></span><span className="step-number">0{index + 1}</span></div><h3>{title}<span>.</span></h3><strong>{subtitle}</strong><p>{copy}</p>{index < 3 && <span className="timeline-arrow"><ArrowRight size={17} /></span>}</article>)}</div></section>
    <section className="privacy-section" id="privacy"><div className="privacy-art"><div className="privacy-art-ring"><ShieldCheck size={60} strokeWidth={1} /><span><Check size={15} /></span></div><span>PERSONAL MOMENTS STAY PERSONAL</span></div><div><div className="eyebrow"><LockKeyhole size={14} /> OUR PRIVACY PROMISE</div><h2>Looking out for you.<br /><em>Respecting your space.</em></h2><p>Your home is your sanctuary. Video is processed locally on the Arduino UNO Q. Only structured event records, such as the time and location of a possible fall, are transmitted to support alerts and historical analysis.</p><div className="privacy-pills"><span><Check size={14} />No video streaming</span><span><Check size={14} />No video uploads</span><span><Check size={14} />Local processing</span></div></div></section>
    <section className="technology-section"><div><div className="eyebrow">QUIETLY WORKING TOGETHER</div><h2>Considered technology. <br />Compassionate care.</h2><p>The right pieces, brought together with people in mind.</p></div><div className="technology-grid">{[{ icon: Radio, title: 'Arduino UNO Q', copy: 'The local home for detection, keeping processing close and personal.' }, { icon: Activity, title: 'Computer vision', copy: 'Recognizes movements that may indicate a fall and records what matters.' }, { icon: Bell, title: 'Caregiver notifications', copy: 'Timely alerts connect a moment of need to the people who can help.' }, { icon: Database, title: 'Databricks', copy: 'Turns event history into patterns that support thoughtful care decisions.' }].map(({ icon: Icon, title, copy }) => <div className="technology-item" key={title}><Icon size={21} strokeWidth={1.5} /><div><h3>{title}</h3><p>{copy}</p></div></div>)}</div></section>
    <section className="about-closing"><Leaf size={27} strokeWidth={1.4} /><h2>More confidence. More connection.<br /><em>More peace of mind.</em></h2><Button asChild><Link to="/">Back to your care space <ArrowRight size={16} /></Link></Button></section>
    <footer className="page-footer"><Brand small /><span>Technology built for the people who care.</span><span>Because every step matters.</span></footer>
  </div>
}

export default App
