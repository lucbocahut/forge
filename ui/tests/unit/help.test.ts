// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { HELP_ASSETS, helpAsset, helpImage } from '../../shared/help-assets'
import { renderMarkdown } from '../../server/utils/markdown'
import { HELP_TOPICS, helpTopic } from '../../shared/help-topics'
import { REPO_DEFAULT_HELP_DOC, helpCandidates, renderHelp, resolveHelpDocPath } from '../../server/utils/help'

const ONBOARDING_MD = fileURLToPath(new URL('../../../docs/onboarding.md', import.meta.url))

describe('renderHelp', () => {
  it('gives every h1/h2/h3 heading a slugified id', () => {
    const { html } = renderHelp('# Set up Forge\n\n## GitHub repository protection\n')
    expect(html).toContain('<h1 id="set-up-forge">Set up Forge</h1>')
    expect(html).toContain('<h2 id="github-repository-protection">GitHub repository protection</h2>')
  })

  it('collects only h1/h2 headings into the toc, in document order', () => {
    const { toc } = renderHelp('# One\n\n## Two\n\n### Three\n\n## Four\n')
    expect(toc).toEqual([
      { id: 'one', text: 'One', level: 1 },
      { id: 'two', text: 'Two', level: 2 },
      { id: 'four', text: 'Four', level: 2 },
    ])
  })

  it('renders paragraphs, bullet lists and numbered lists', () => {
    const { html } = renderHelp('A paragraph.\n\n- First\n- Second\n\n1. Step one\n2. Step two\n')
    expect(html).toContain('<p>A paragraph.</p>')
    expect(html.replace(/\n/g, '')).toContain('<ul><li>First</li><li>Second</li></ul>')
    expect(html.replace(/\n/g, '')).toContain('<ol><li>Step one</li><li>Step two</li></ol>')
  })

  it('renders a 4-space-indented code block and inline code', () => {
    const { html } = renderHelp('Run `bin/forge setup` then:\n\n    forge service --root /srv/forge\n')
    expect(html).toContain('<code>bin/forge setup</code>')
    expect(html).toContain('<pre><code>forge service --root /srv/forge\n</code></pre>')
  })

  it('rewrites a relative *.md link to the GitHub blob URL', () => {
    const { html } = renderHelp('See [installation](installation.md) for details.\n')
    expect(html).toContain('href="https://github.com/lucbocahut/forge/blob/main/docs/installation.md"')
    expect(html).toContain('>installation</a>')
  })

  it('leaves an absolute http(s) link alone', () => {
    const { html } = renderHelp('[GitHub docs](https://docs.github.com/en/rulesets)\n')
    expect(html).toContain('href="https://docs.github.com/en/rulesets"')
  })

  it('renders a javascript: link as plain text', () => {
    const { html } = renderHelp('[Click me](javascript:alert(1))\n')
    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
    expect(html).toContain('Click me')
  })

  it('renders a relative link that is not a bare *.md filename as plain text', () => {
    const { html } = renderHelp('[Settings](/settings)\n')
    expect(html).not.toContain('href=')
    expect(html).toContain('Settings')
  })

  it('strips an embedded <img onerror> to inert text, never emitting the tag', () => {
    const { html } = renderHelp('Before <img src=x onerror="window.pwned=1"> after\n')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('onerror=1')
    expect(html).toContain('&lt;img')
    expect(html).toContain('Before')
    expect(html).toContain('after')
  })

  it('never emits a script tag however the doc smuggles one in', () => {
    const { html } = renderHelp('Before\n\n<script>window.injected=1</script>\n\nAfter\n')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('window.injected=1</script>')
  })

  it('preserves every heading linked from the current UI', () => {
    const source = readFileSync(ONBOARDING_MD, 'utf8')
    const { html, toc } = renderHelp(source)
    expect(html).toContain('id="configuration-and-recovery"')
    expect(html).toContain('id="host-installation"')
    expect(html).toContain('id="github-repository-protection"')
    expect(toc.map((entry) => entry.id)).toContain('github-repository-protection')
    expect(toc.map((entry) => entry.id)).toContain('host-installation')
  })
})

// The wiring between the route's raw inputs and resolveHelpDocPath's three
// candidates — where an override once swallowed the repo default.
describe('helpCandidates', () => {
  const cwd = '/srv/app/ui'
  const repo = '/srv/app/docs/onboarding.md'

  it('keeps the repo doc as the last candidate even when helpDoc is overridden', () => {
    const candidates = helpCandidates({ helpDoc: '/etc/custom.md', forgeRoot: null, cwd })
    expect(candidates.nitroHelpDoc).toBe('/etc/custom.md')
    // The point of the test: an override that names a file which is not there
    // must still fall through to $FORGE_ROOT and then to the repo copy.
    expect(candidates.repoDefault).toBe(repo)
  })

  it('resolves a relative override against cwd, repo default unchanged', () => {
    const candidates = helpCandidates({ helpDoc: 'tests/e2e/fixtures/help.md', forgeRoot: null, cwd })
    expect(candidates.nitroHelpDoc).toBe('/srv/app/ui/tests/e2e/fixtures/help.md')
    expect(candidates.repoDefault).toBe(repo)
  })

  it('treats the declared default as no override at all, so $FORGE_ROOT still wins', () => {
    const candidates = helpCandidates({ helpDoc: REPO_DEFAULT_HELP_DOC, forgeRoot: '/srv/forge', cwd })
    expect(candidates.nitroHelpDoc).toBeNull()
    expect(candidates.forgeRoot).toBe('/srv/forge')
    expect(candidates.repoDefault).toBe(repo)
  })

  it('treats a missing, empty or blank helpDoc as the default', () => {
    for (const helpDoc of [undefined, null, '', '   ']) {
      expect(helpCandidates({ helpDoc, cwd }).nitroHelpDoc).toBeNull()
      expect(helpCandidates({ helpDoc, cwd }).repoDefault).toBe(repo)
    }
  })

  it('normalises a blank FORGE_ROOT to null', () => {
    expect(helpCandidates({ forgeRoot: '  ', cwd }).forgeRoot).toBeNull()
    expect(helpCandidates({ forgeRoot: ' /srv/forge ', cwd }).forgeRoot).toBe('/srv/forge')
  })

  // The regression, end to end through the pure pair: with the override
  // unreadable, resolution must still reach the repo doc.
  it('falls through an unreadable override to the repo default', () => {
    const candidates = helpCandidates({ helpDoc: '/etc/custom.md', forgeRoot: null, cwd })
    expect(resolveHelpDocPath(candidates, (path) => path === repo)).toBe(repo)
  })
})

describe('resolveHelpDocPath', () => {
  const repoDefault = '/repo/docs/onboarding.md'

  it('prefers NITRO_HELP_DOC when it is readable, even with FORGE_ROOT set', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/custom/help.md' || path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/custom/help.md')
  })

  it('falls through an unreadable NITRO_HELP_DOC to FORGE_ROOT', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/srv/forge/docs/onboarding.md')
  })

  it('uses FORGE_ROOT over the repo default when NITRO_HELP_DOC is unset', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: null, forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/srv/forge/docs/onboarding.md')
  })

  it('falls through to the repo default when nothing else is readable', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === repoDefault,
    )
    expect(resolved).toBe(repoDefault)
  })

  it('returns null when no candidate is readable', () => {
    const resolved = resolveHelpDocPath({ nitroHelpDoc: null, forgeRoot: null, repoDefault }, () => false)
    expect(resolved).toBeNull()
  })
})


describe('operator help guides', () => {
  it('only accepts catalog IDs, never browser-supplied paths', () => {
    for (const value of ['../package.json', '/etc/passwd', '', ['workflow'], 'onboarding.md']) {
      expect(helpTopic(value)).toBeUndefined()
    }
    expect(helpTopic(undefined)?.id).toBe('setup')
    expect(() => helpCandidates({ cwd: '/repo/ui', topic: '../secret' })).toThrow('Unknown help topic')
  })

  it('resolves a guide independently of the setup-only override', () => {
    const candidates = helpCandidates({ cwd: '/repo/ui', helpDoc: '/custom/setup.md', forgeRoot: '/srv/forge', topic: 'workflow' })
    expect(candidates.nitroHelpDoc).toBeNull()
    expect(candidates.repoDefault).toBe('/repo/docs/issue-state-machine.md')
    expect(resolveHelpDocPath(candidates, p => p.startsWith('/srv/forge/'))).toBe('/srv/forge/docs/issue-state-machine.md')
    expect(resolveHelpDocPath(candidates, p => p.startsWith('/repo/'))).toBe('/repo/docs/issue-state-machine.md')
  })

  it('keeps guide links and fragments in Forge, with source links on GitHub', () => {
    const { html } = renderHelp('[states](issue-state-machine.md#when-progress-stops) [source](../forge/factory/contracts.py) [here](#main-path) [bad](//evil.example)')
    expect(html).toContain('href="/setup-help?topic=workflow#when-progress-stops"')
    expect(html).toContain('href="https://github.com/lucbocahut/forge/blob/main/forge/factory/contracts.py"')
    expect(html).toContain('href="#main-path"')
    expect(html).not.toContain('href="//')
  })

  it('renders rich Markdown while keeping diagram source separate from HTML', () => {
    const source = '# Flow\n\n**Progress**\n\n| Stage | Next |\n| --- | --- |\n| ready | Build |\n\n```mermaid\nflowchart TD\n A --> B\n```\n\n## Flow\n\nAfter'
    const doc = renderHelp(source)
    expect(doc.html).toContain('<strong>Progress</strong>')
    expect(doc.html).toContain('<div class="help-table"><table>')
    expect(doc.blocks[1]).toEqual({ diagram: 'flowchart TD\n A --> B\n', title: 'Flow' })
    expect(doc.toc.map(x => x.id)).toEqual(['flow', 'flow-1'])
    expect(doc.blocks[2]).toHaveProperty('html', expect.stringContaining('After'))
  })

  it('ships concise illustrated guides with at least eight strict-mode diagrams', () => {
    let diagrams = 0
    const used = new Set<string>()
    for (const topic of HELP_TOPICS) {
      const source = readFileSync(fileURLToPath(new URL(`../../../docs/${topic.file}`, import.meta.url)), 'utf8')
      const doc = renderHelp(source)
      expect(doc.toc.length).toBeGreaterThan(1)
      expect(doc.html).not.toContain('<script')
      const count = doc.blocks.filter(b => 'diagram' in b).length
      diagrams += count
      expect(count).toBeGreaterThanOrEqual(2)
      expect(source.split(/\s+/).length).toBeLessThan(topic.id === 'setup' || topic.id === 'runtime' ? 1600 : 900)
      for (const match of doc.html.matchAll(/src="\/_ui\/help-assets\/([a-z-]+\.svg)"/g)) used.add(match[1]!)
      expect(doc.html).toContain('<img')
    }
    expect(diagrams).toBeGreaterThanOrEqual(8)
    expect(used.size).toBeGreaterThanOrEqual(6)
    expect([...used].sort()).toEqual(Object.keys(HELP_ASSETS).sort())
  })
})


describe('Help image boundary', () => {
  it('renders a bundled relative image with meaningful alt, fixed dimensions and lazy loading', () => {
    const { html } = renderHelp('![Four role cubes](assets/setup-hero.svg)')
    expect(html).toContain('<img src="/_ui/help-assets/setup-hero.svg" alt="Four role cubes" width="600" height="340" loading="lazy" decoding="async"')
    expect(renderHelp('![Roles](/_ui/help-assets/setup-hero.svg)').html).toContain('<img')
    expect(renderHelp('![](assets/setup-hero.svg)').html).not.toContain('<img')
  })

  it('discards external, data, protocol-relative and non-catalog image sources', () => {
    for (const src of ['https://tracker.example/pixel.svg', 'http://example.test/a.png',
      '//tracker.example/a.svg', 'data:image/svg+xml;base64,PHN2Zz4=',
      'assets/unknown.svg', 'assets/../setup-hero.svg', 'assets/%2e%2e/setup-hero.svg',
      '/_ui/help-assets/setup-hero.svg?tracking=yes', 'assets/setup-hero.svg#tracking',
      'assets/constructor', 'assets/__proto__', '../docs/assets/setup-hero.svg']) {
      expect(helpImage(src), src).toBeNull()
      expect(renderHelp(`![Tracking](${src})`).html, src).not.toContain('<img')
    }
  })

  it('does not extend image permissions to issue Markdown', () => {
    for (const src of ['assets/setup-hero.svg', '/_ui/help-assets/setup-hero.svg', 'https://tracker.example/pixel.svg']) {
      expect(renderMarkdown(`![No image](${src})`, 'lucbocahut/forge')).not.toContain('<img')
    }
  })

  it('uses a finite catalog with self-contained, accessible light/dark SVGs', () => {
    for (const name of ['../setup-hero.svg', '/etc/passwd', 'constructor', '__proto__', '', ['setup-hero.svg']]) {
      expect(helpAsset(name)).toBeNull()
    }
    for (const [name, size] of Object.entries(HELP_ASSETS)) {
      const source = readFileSync(fileURLToPath(new URL(`../../../docs/assets/${name}`, import.meta.url)), 'utf8')
      expect(source).toContain(`viewBox="0 0 ${size.width} ${size.height}"`)
      expect(source).toContain('<title id="title">')
      expect(source).toContain('<desc id="desc">')
      expect(source).toContain('prefers-color-scheme:dark')
      expect(source).not.toMatch(/<script|<foreignObject|(?:href|src)=|url\(|\son[a-z]+=/i)
      expect(size.width).toBeLessThanOrEqual(1200)
      expect(size.height).toBeLessThanOrEqual(1200)
    }
  })
})
