<script setup lang="ts">
interface Updates {
  installed_revision: string | null
  latest: { revision: string; published_at: string; url: string } | null
  status: 'current' | 'available' | 'ahead' | 'diverged' | 'unknown' | 'unavailable' | 'no_release'
  checked_at: number
  error: string | null
  releases_url: string
}
const api = useForgeApi()
const localTime = useLocalTime()
const checking = ref(false)
const updates = useResource<Updates>('/api/updates', path => api.get<Updates>(path + (checking.value ? '?refresh=1' : '')),
  { server: false, interval: false, maxAge: 3600000 })
onMounted(updates.start)
const current = computed(() => updates.data.value)
const message = computed(() => updates.error.value ? 'Could not check for updates.' : current.value?.error ?? ({
  current: 'You’re up to date.', available: 'An update is available.', ahead: 'You’re running a newer development version.',
  diverged: 'This build differs from the published release.', unknown: 'The installed version could not be compared.',
  unavailable: 'Could not check for updates.', no_release: 'No published builds are available yet.',
}[current.value?.status ?? 'unavailable']))
async function check() {
  checking.value = true
  try { await updates.refresh() } finally { checking.value = false }
}
</script>
<template>
  <section id="updates" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-sm font-semibold text-white">Updates</h2>
      <UButton class="min-h-11" icon="i-lucide-refresh-cw" color="neutral" variant="outline" :loading="checking" :disabled="checking" @click="check">Check for updates</UButton>
    </div>
    <p role="status" class="mt-2 text-sm" :class="current?.status === 'available' && !updates.error.value ? 'text-mint-200' : 'text-muted'">
      {{ !current && !updates.error.value ? 'Checking GitHub…' : message }}
    </p>
    <p v-if="current" class="mt-2 text-xs text-muted">
      Installed: {{ current.installed_revision?.slice(0, 12) || 'Unknown' }}<template v-if="current.latest"> · Latest: {{ current.latest.revision.slice(0, 12) }}</template>
    </p>
    <p v-if="current" class="mt-1 text-xs text-muted">Checked {{ localTime(current.checked_at) }}</p>
    <div class="mt-3 flex flex-wrap gap-4 text-sm">
      <a :href="current?.latest?.url || 'https://github.com/lucbocahut/forge/releases'" target="_blank" rel="noopener noreferrer" class="text-sky-200 hover:underline">{{ current?.latest ? 'Release notes ↗' : 'GitHub releases ↗' }}</a>
      <a v-if="current?.status === 'available'" href="https://github.com/lucbocahut/forge/blob/main/docs/store-migration.md#upgrade-an-installation" target="_blank" rel="noopener noreferrer" class="text-sky-200 hover:underline">Update instructions ↗</a>
    </div>
  </section>
</template>
