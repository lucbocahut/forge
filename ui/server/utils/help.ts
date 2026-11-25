// Render installation-owned guides with the same Markdown parser and HTML
// text allowlist as issue descriptions, plus catalogued Help images only.
// Diagrams remain plain source data until the
// browser renders them with Mermaid's strict mode; raw document HTML is disabled.
import path from 'node:path'
import MarkdownIt from 'markdown-it'
import sanitizeHtml from 'sanitize-html'
import { MARKDOWN_ALLOWLIST } from './markdown'
import { helpImage } from '../../shared/help-assets'
import { HELP_TOPICS, helpTopic, helpTopicHref } from '../../shared/help-topics'

type TocEntry = { id: string; text: string; level: 1 | 2 }
export type HelpBlock = { html: string } | { diagram: string; title: string }
export type HelpDoc = { html: string; toc: TocEntry[]; blocks: HelpBlock[] }

const DOCS_BASE = 'https://github.com/lucbocahut/forge/blob/main/docs/'
const ALLOWED_ATTRIBUTES = {
  ...Object.fromEntries(Object.entries(MARKDOWN_ALLOWLIST.attributes).filter(([tag]) => tag !== 'input')),
  h1: ['id'], h2: ['id'], h3: ['id'], h4: ['id'], h5: ['id'], h6: ['id'],
  div: ['class'],
  img: ['src', 'alt', 'width', 'height', 'loading', 'decoding'],
}

function resolveHref(href: string): string | null {
  if (/^#[a-z0-9-]+$/.test(href)) return href
  const local = /^([a-z-]+\.md)(#[a-z0-9-]+)?$/.exec(href)
  if (local) {
    const topic = HELP_TOPICS.find(topic => topic.file === local[1])
    return topic ? helpTopicHref(topic.id) + (local[2] ?? '') : DOCS_BASE + href
  }
  // Source-code references in a guide remain on the project's GitHub tree.
  if (/^\.\.\/(forge|deploy|ui)\/[a-zA-Z0-9_./-]+$/.test(href)) {
    const resolved = new URL(href, DOCS_BASE)
    return resolved.pathname.startsWith('/lucbocahut/forge/blob/main/') ? resolved.href : null
  }
  try {
    const url = new URL(href)
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null
  } catch { return null }
}

function sanitize(html: string): string {
  return sanitizeHtml(html, {
    allowedTags: [...MARKDOWN_ALLOWLIST.tags.filter(tag => tag !== 'input'), 'div', 'img'],
    allowedAttributes: ALLOWED_ATTRIBUTES,
    allowedSchemes: ['http', 'https'],
    disallowedTagsMode: 'discard',
    transformTags: {
      img: (_tagName, attribs): sanitizeHtml.Tag => {
        const asset = helpImage(attribs.src ?? '')
        const alt = (attribs.alt ?? '').trim()
        if (!asset || !alt) return { tagName: 'span', attribs: {} }
        return { tagName: 'img', attribs: {
          src: `/_ui/help-assets/${asset.name}`, alt,
          width: String(asset.width), height: String(asset.height), loading: 'lazy', decoding: 'async',
        } }
      },
      a: (tagName, attribs): sanitizeHtml.Tag => {
        const href = resolveHref(attribs.href ?? '')
        if (!href) return { tagName: 'span', attribs: {} }
        return { tagName, attribs: { href } }
      },
    },
  }).trim()
}

export function renderHelp(markdown: string): HelpDoc {
  const md = new MarkdownIt({ html: false, linkify: false })
  md.renderer.rules.table_open = () => '<div class="help-table"><table>'
  md.renderer.rules.table_close = () => '</table></div>'
  const tokens = md.parse(markdown, {})
  const toc: TocEntry[] = []
  const blocks: HelpBlock[] = []
  const counts = new Map<string, number>()
  let section = 'Diagram'
  let start = 0
  const htmlParts: string[] = []
  function flush(end: number) {
    const html = sanitize(md.renderer.render(tokens.slice(start, end), md.options, {}))
    if (html) { blocks.push({ html }); htmlParts.push(html) }
  }
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index]!
    if (token.type === 'heading_open') {
      const inline = tokens[index + 1]
      const text = inline?.children?.filter(child => child.type !== 'html_inline').map(child => child.content).join('') || inline?.content || ''
      const slug = text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'section'
      const count = counts.get(slug) ?? 0
      counts.set(slug, count + 1)
      const id = count ? `${slug}-${count}` : slug
      token.attrSet('id', id)
      const level = Number(token.tag.slice(1))
      if (level <= 2) toc.push({ id, text, level: level as 1 | 2 })
      section = text
    }
    if (token.type === 'fence' && token.info.trim() === 'mermaid') {
      flush(index)
      blocks.push({ diagram: token.content, title: section })
      // Preserve a non-JavaScript/older-client representation in the API.
      htmlParts.push(sanitize(md.renderer.render([token], md.options, {})))
      start = index + 1
    }
  }
  flush(tokens.length)
  return { html: htmlParts.join(''), toc, blocks }
}

// --- Document resolution (pure; the route supplies the real inputs and I/O) ---

type HelpDocCandidates = {
  nitroHelpDoc: string | null
  forgeRoot: string | null
  repoDefault: string
}

// Precedence (ruled): NITRO_HELP_DOC, else $FORGE_ROOT/docs/onboarding.md,
// else the repo's own ../docs/onboarding.md — the first *readable* one wins,
// because the installation copy at $FORGE_ROOT overrides the repo file only
// when it actually exists. `isReadable` is injected so this stays pure and
// unit-testable without touching a filesystem; the route passes a real check.
// The runtimeConfig default for `helpDoc`, and — separately — the last-resort
// candidate. `cwd` is `ui/` when Nitro runs (the e2e harness spawns the built
// server with that as its cwd), which is what makes this relative path the
// repo's own `docs/onboarding.md`.
export const REPO_DEFAULT_HELP_DOC = '../docs/onboarding.md'

// Turns the route's raw inputs into the three candidates above, so the wiring
// is testable without a filesystem.
//
// `helpDoc` is the declared runtime config value; `NITRO_HELP_DOC` is only the
// env override Nitro maps onto that key. It counts as an override — and so
// outranks $FORGE_ROOT — only when it differs from the declared default.
//
// `repoDefault` is always the repo doc, never the override: an override
// naming a file that is missing (a typo, a document not copied to the live
// root yet) has to fall through to $FORGE_ROOT and then to the repo copy,
// which it cannot do if it is also the last candidate.
export function helpCandidates(
  { helpDoc, forgeRoot, cwd, topic = 'setup' }: { helpDoc?: string | null; forgeRoot?: string | null; cwd: string; topic?: string },
): HelpDocCandidates {
  const guide = helpTopic(topic)
  if (!guide) throw new Error('Unknown help topic')
  const configured = String(helpDoc ?? '').trim() || REPO_DEFAULT_HELP_DOC
  return {
    nitroHelpDoc: topic !== 'setup' || configured === REPO_DEFAULT_HELP_DOC ? null : path.resolve(cwd, configured),
    forgeRoot: String(forgeRoot ?? '').trim() || null,
    repoDefault: path.resolve(cwd, '../docs', guide.file),
  }
}

export function resolveHelpDocPath(candidates: HelpDocCandidates, isReadable: (path: string) => boolean): string | null {
  const ordered = [
    candidates.nitroHelpDoc,
    candidates.forgeRoot ? `${candidates.forgeRoot.replace(/\/+$/, '')}/docs/${path.basename(candidates.repoDefault)}` : null,
    candidates.repoDefault,
  ].filter((path): path is string => Boolean(path))
  return ordered.find((path) => isReadable(path)) ?? null
}
