import type { OnboardingView } from '~/utils/onboarding'

// Route redirects mirroring the Python 303s (contracts §2, "Pages/assets
// served"): an operator who is mid-setup, or mid-reset, is always sent back
// to the one page that can move them forward.
const SETUP_PAGES = ['/login', '/onboarding', '/reset', '/setup-help', '/installation']
// Contracts §1, GET guard 2. /api/onboarding is exempt from the fence, but
// letting the reason surface as a page error would be a dead end anyway.
const RESET_FENCE = 'Factory reset is in progress'

type Payload = OnboardingView | { ok: false; reason: string }

export default defineNuxtRouteMiddleware(async (to) => {
  if (SETUP_PAGES.includes(to.path)) return
  // useRequestFetch so the SSR call carries the browser's Host and Cookie
  // through the proxy to Python.
  const request = useRequestFetch()
  const resources = useResources()

  let payload: Payload
  try {
    const entry = resources.entry<Payload>('/api/onboarding')
    const cached = entry.data.value
    const refresh = () => resources.load('/api/onboarding', () => request<Payload>('/api/onboarding', { headers: { accept: 'application/json' }, timeout: 5000 }), 5000)
    // Known-active navigation never waits for a background readiness read.
    // Explicit lifecycle invalidation sets updatedAt to zero and forces a check.
    if (cached && cached.ok !== false && cached.phase === 'active' && entry.updatedAt.value) {
      payload = cached
      void refresh()
    } else {
      const value = await refresh()
      if (!value) return
      payload = value
    }
  } catch {
    // Setup state is unreachable: the page renders its own connection error
    // rather than being bounced somewhere that cannot load either.
    return
  }

  const onboarding = () => navigateTo('/onboarding', { redirectCode: 303 })

  if (!payload || payload.ok === false) {
    return payload?.reason === RESET_FENCE ? onboarding() : undefined
  }
  if (payload.phase === 'resetting') return onboarding()

  // `/` belongs to the started factory, so every phase short of `active`
  // is sent back to guided setup.
  if (to.path === '/' && payload.phase !== 'active') return onboarding()

  // The /settings redirect uses the `active` flag from GET /api/factory.
  // A factory can already be running while a *new* guided setup is under way,
  // and Settings then renders with its capacity form locked ("Use guided
  // setup until the factory has started"). Only that case needs the second
  // read, so an activated setup never pays for it.
  if (to.path === '/settings') {
    if (payload.phase === 'active') return
    let factory: { ok?: boolean; active?: boolean }
    try {
      factory = await request<{ ok?: boolean; active?: boolean }>('/api/factory', { headers: { accept: 'application/json' }, timeout: 5000 })
    } catch {
      return
    }
    if (factory?.ok === false || factory?.active !== true) return onboarding()
  }
})
