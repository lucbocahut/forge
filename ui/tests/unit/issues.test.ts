import { describe, it, expect } from 'vitest'
import {
  forgeReportUrl, actionEffect, ACTION_EFFECTS, ACTION_LABELS, EVENT_LABELS, SHOW_OPTIONS, attentionNote,
  decisionNeedsReason, emptyState, filterQuery, filtersFromQuery, freshnessText, listParams, preselectedReviewers,
  attemptsText, heldByLines, pullLabel, reviewPull, reviewRequestText, reviewRows, reviewerOptions, reviewerReason, rowAction, summaryBlock,
  activityAge, rowFacts, rowPrimary, rowStory, blockingIssues,
  type IssueListResponse, type IssueRow,
} from '../../app/utils/issues'

const scope = { id: 1, name: 'XRPL-Commons/academy', complete: true, fresh: true, at: 1790000000, error: null }
const list = (over: Partial<IssueListResponse> = {}): IssueListResponse => ({
  ok: true, controls_token: 'synthetic', issues: [], total: 0, page: 1, pages: 1,
  repositories: [scope], complete: true, actions: [], ...over,
})
const row = (over: Partial<IssueRow> = {}): IssueRow => ({
  key: '1:610', repo: 'XRPL-Commons/academy', repo_id: 1, number: 610, title: 'Make course navigation easier',
  content_hash: 'h', body_truncated: false, state: 'open', updated_at: null, labels: [], assignees: [],
  status: 'backlog', summary: null, explanation: 'Not queued for Forge.', actions: ['queue'], setup_needed: false,
  environment_blocker: null, pull_requests: [], dependencies: [], fresh: true, revision: 'current',
  setup_url: '/settings/repositories/1?setup=1', active: null, ...over,
})

describe('copy maps', () => {
  it('labels every decision, including stop', () => {
    expect(ACTION_LABELS.queue).toBe('Queue for Forge')
    expect(ACTION_LABELS.backlog).toBe('Return to backlog')
    expect(ACTION_LABELS.pause).toBe('Pause')
    expect(ACTION_LABELS.resume).toBe('Release hold')
    expect(ACTION_LABELS.retry).toBe('Retry attempt')
    expect(ACTION_LABELS.recheck).toBe('Recheck readiness')
    expect(ACTION_LABELS.stop).toBe('Stop attempt')
  })
  it('offers a distinct safe exit for an external change', () => {
    const changed = row({ status: 'attention', actions: ['recheck'], reason_code: 'external_change' })
    expect(rowAction(changed, null)).toBe('Reload and continue')
    expect(actionEffect('recheck', changed)).toContain('repeats review and audit')
    expect(actionEffect('recheck', changed)).toContain('Other holds remain')
  })
  it('explains the effect of every decision', () => {
    expect(Object.keys(ACTION_EFFECTS).sort()).toEqual(Object.keys(ACTION_LABELS).sort())
    expect(ACTION_EFFECTS.stop).toBe('This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.')
    expect(ACTION_EFFECTS.backlog).toBe('This removes the issue from Forge’s queue. The GitHub issue stays open.')
  })
  it('labels ledger events for the activity list', () => {
    expect(EVENT_LABELS.discovered).toBe('Added to Forge')
    expect(EVENT_LABELS.operator_conflict).toBe('Decision needs another review')
    expect(EVENT_LABELS.cancelled).toBe('Attempt stopped')
  })
  it('offers every issue view in the Show select, open first', () => {
    expect(SHOW_OPTIONS).toEqual([
      { value: 'open', label: 'Open issues' },
      { value: 'attention', label: 'Needs attention' },
      { value: 'backlog', label: 'Backlog' },
      { value: 'queued', label: 'Queued' },
      { value: 'working', label: 'Working' },
      { value: 'waiting', label: 'Waiting' },
      { value: 'blocked', label: 'Waiting on other issues' },
      { value: 'completed', label: 'Completed' },
      { value: 'dropped', label: 'No longer needed' },
    ])
  })
})

describe('listParams', () => {
  it('serializes every filter, trimming the search text', () => {
    expect(listParams({ state: 'open', repoId: 2, q: ' logging ', page: 3 })).toBe('state=open&page=3&repo_id=2&q=logging')
  })
  it('omits an absent repository and an empty search', () => {
    expect(listParams({ state: 'completed', repoId: null, q: '   ', page: 1 })).toBe('state=completed&page=1')
  })
  it('escapes a search that would otherwise break the query', () => {
    expect(listParams({ state: 'open', repoId: null, q: 'a&b c', page: 1 })).toBe('state=open&page=1&q=a%26b+c')
  })
})

describe('rowAction', () => {
  it('announces a decision that is still being applied', () => {
    expect(rowAction(row({ status: 'attention' }), { id: 'a1', key: '1:610', status: 'pending', message: null })).toBe('Applying…')
    expect(rowPrimary(row({ status: 'attention', actions: ['retry'] }), { id: 'a1', key: '1:610', status: 'pending', message: null }))
      .toEqual({ label: 'Applying…', decision: false })
  })
  it('names the decision the issue is waiting for', () => {
    expect(rowPrimary(row({ status: 'backlog', actions: ['queue'] }), null)).toEqual({ label: 'Queue for Forge', decision: true })
    expect(rowAction(row({ status: 'attention', actions: ['retry'] }), null)).toBe('Retry attempt')
    expect(rowAction(row({ status: 'attention', actions: ['resume'] }), null)).toBe('Release hold')
    expect(rowAction(row({ status: 'attention', actions: ['recheck'] }), null)).toBe('Recheck readiness')
    expect(rowAction(row({ status: 'attention', actions: ['retry', 'assign_reviewer'] }), null)).toBe('Assign reviewer')
  })
  it('never leads with backing out of a queued issue', () => {
    expect(rowPrimary(row({ status: 'queued', actions: ['backlog', 'pause'] }), null)).toEqual({ label: 'Open', decision: false })
  })
  it('sends a working issue to its progress, an attention issue to a review', () => {
    expect(rowAction(row({ status: 'working', actions: [] }), null)).toBe('View progress')
    expect(rowAction(row({ status: 'attention', actions: [] }), null)).toBe('Review')
  })
  it('falls back to opening the issue', () => {
    expect(rowAction(row({ status: 'completed', actions: [] }), null)).toBe('Open')
    expect(rowAction(row({ status: 'waiting', actions: [] }), null)).toBe('Open')
  })
})

describe('rowStory', () => {
  it('leads with the headline and adds the plain sentences', () => {
    expect(rowStory(row({ summary: { headline: 'Built and waiting for review.', plain: 'A reviewer checks it next.' } })))
      .toEqual({ headline: 'Built and waiting for review.', plain: 'A reviewer checks it next.' })
  })
  it('falls back to the explanation and never repeats itself', () => {
    expect(rowStory(row())).toEqual({ headline: 'Not queued for Forge.', plain: '' })
    expect(rowStory(row({ summary: { headline: 'Same.', plain: 'Same.' } }))).toEqual({ headline: 'Same.', plain: '' })
    expect(rowStory(row({ summary: { headline: '  ', plain: 'Only plain.' }, explanation: 'Queued.' })))
      .toEqual({ headline: 'Queued.', plain: 'Only plain.' })
  })
})

describe('activityAge', () => {
  const now = Date.parse('2026-09-20T12:00:00Z') / 1000
  it('says how long ago GitHub last saw activity', () => {
    expect(activityAge('2026-09-20T09:00:00Z', now)).toBe('Updated 3 h ago')
    expect(activityAge('2026-09-17T12:00:00Z', now)).toBe('Updated 3 d ago')
    expect(activityAge('2026-09-20T11:59:30Z', now)).toBe('Updated just now')
  })
  it('says nothing about an unknown time', () => {
    expect(activityAge(null, now)).toBe('')
    expect(activityAge('not a date', now)).toBe('')
  })
})

describe('rowFacts', () => {
  const now = Date.parse('2026-09-20T12:00:00Z') / 1000
  it('lists only the facts the row carries, in reading order', () => {
    expect(rowFacts(row(), now)).toEqual([])
    const facts = rowFacts(row({
      pull_requests: [
        { number: 88, title: 'a', kind: 'candidate' },
        { number: 71, title: 'b', kind: 'forge_attempt' },
        { number: 40, title: 'c', kind: 'external' },
        { number: 12, title: 'd' },
      ],
      attempts: { failed: 2, limit: 3 },
      dependencies: [584, 590],
      held_by: [{ label: 'hold', login: 'maintainer', at: '2026-09-19T10:00:00Z' }, { label: 'hold', login: null, at: 'x' }],
      updated_at: '2026-09-20T10:00:00Z',
    }), now)
    expect(facts.map(fact => fact.text)).toEqual([
      'PR #88', 'Earlier Forge draft #71', 'Other PR #40', 'PR #12',
      '2 of 3 attempts failed', 'Depends on #584, #590', 'On hold: hold', 'Updated 2 h ago',
    ])
    expect(new Set(facts.map(fact => fact.key)).size).toBe(facts.length)
  })
  it('leaves out attempts that never failed', () => {
    expect(rowFacts(row({ attempts: { failed: 0, limit: 3 } }), now)).toEqual([])
  })
})

describe('freshnessText', () => {
  it('reports a scope error verbatim', () => {
    const error = 'Could not finish refreshing. Previously received issues are still shown.'
    expect(freshnessText(list({ repositories: [{ ...scope, error }] }))).toBe(error)
  })
  it('confirms a complete inventory and that viewing is not queuing', () => {
    expect(freshnessText(list())).toBe('Up to date · Viewing issues never queues them for work.')
  })
  it('says an incomplete inventory is still being received', () => {
    expect(freshnessText(list({ complete: false }))).toBe('Showing received issues. The GitHub inventory is still incomplete or needs refreshing.')
  })
  it('says nothing when no repository is configured', () => {
    expect(freshnessText(list({ repositories: [] }))).toBe('')
  })
})

describe('emptyState', () => {
  it('sends an operator with no repositories to repository setup', () => {
    expect(emptyState(list({ repositories: [] }))).toEqual({
      title: 'Choose where Forge should work',
      text: 'Add and enable a repository to see its issues here.',
      link: { to: '/settings/repositories?add=1', label: 'Add repository →' },
    })
  })
  it('keeps received issues visible while the inventory is incomplete', () => {
    expect(emptyState(list({ complete: false }))).toEqual({
      title: 'Loading the issue list',
      text: 'Previously received issues remain visible while Forge checks GitHub.',
    })
  })
  it('blames the filter when the inventory is complete', () => {
    expect(emptyState(list())).toEqual({ title: 'No issues in this view', text: 'Try another filter.' })
  })
  it('is absent while there are rows to show', () => {
    expect(emptyState(list({ issues: [row()], total: 1 }))).toBeNull()
  })
})

describe('decisionNeedsReason', () => {
  it('requires a written reason for a hold, a retry or a stop', () => {
    expect(decisionNeedsReason('pause')).toBe(true)
    expect(decisionNeedsReason('resume')).toBe(true)
    expect(decisionNeedsReason('retry')).toBe(true)
    expect(decisionNeedsReason('stop')).toBe(true)
  })
  it('does not for a queue, a backlog or a recheck', () => {
    expect(decisionNeedsReason('queue')).toBe(false)
    expect(decisionNeedsReason('backlog')).toBe(false)
    expect(decisionNeedsReason('recheck')).toBe(false)
  })
})

describe('pull requests and attempts', () => {
  it('labels each pull request by what it is to the issue', () => {
    expect(pullLabel({ number: 31, title: '', kind: 'candidate' })).toBe('PR #31')
    expect(pullLabel({ number: 30, title: '', kind: 'forge_attempt' })).toBe('Earlier Forge draft #30')
    expect(pullLabel({ number: 29, title: '', kind: 'external' })).toBe('Other PR #29')
    expect(pullLabel({ number: 28, title: '' })).toBe('PR #28')
  })

  it('requests the review on the candidate, never on someone else’s pull request', () => {
    expect(reviewPull([{ number: 29, title: '', kind: 'external' }, { number: 31, title: '', kind: 'candidate' }])?.number).toBe(31)
    expect(reviewPull([{ number: 29, title: '', kind: 'external' }])).toBeNull()
    expect(reviewPull(undefined)).toBeNull()
  })

  it('says which label paused the issue, who applied it and when', () => {
    expect(heldByLines([
      { label: 'forge:hold', login: 'maintainer', at: '2026-09-21T09:00:00Z' },
      { label: 'blocked', login: null, at: '2026-01-05T23:30:00Z' },
    ])).toEqual([
      'Labelled forge:hold by @maintainer on Sep 21',
      'Labelled blocked on Jan 5',
    ])
    expect(heldByLines([{ label: 'blocked', login: 'x', at: 'not a date' }])).toEqual(['Labelled blocked by @x'])
    expect(heldByLines(null)).toEqual([])
    expect(heldByLines(undefined)).toEqual([])
  })

  it('counts failed attempts only once one has failed', () => {
    expect(attemptsText({ failed: 2, limit: 3 })).toBe('2 of 3 attempts failed')
    expect(attemptsText({ failed: 0, limit: 3 })).toBe('')
    expect(attemptsText(null)).toBe('')
    expect(attemptsText(undefined)).toBe('')
  })
})

describe('reviewRows', () => {
  it('names the role, the outcome and the lead sentence of each review', () => {
    const rows = reviewRows([
      { role: 'review', outcome: 'pass', summary: 'The change is correct. It adds tests for the redaction helper and keeps the public API stable.' },
      { role: 'audit', outcome: 'reject', summary: 'The install token is still written to the build log.' },
    ])
    expect(rows).toEqual([
      {
        role: 'review', label: 'Review', outcome: 'passed', outcomeLabel: 'Passed', tone: 'success',
        lead: 'The change is correct.',
        summary: 'The change is correct. It adds tests for the redaction helper and keeps the public API stable.',
        more: true,
      },
      {
        role: 'audit', label: 'Security review', outcome: 'rejected', outcomeLabel: 'Rejected', tone: 'error',
        lead: 'The install token is still written to the build log.',
        summary: 'The install token is still written to the build log.',
        more: false,
      },
    ])
  })

  it('names the reviewer’s own verdict, not the issue’s status', () => {
    expect(reviewRows([{ role: 'review', outcome: 'unable', summary: 'The checkout would not build.' }])[0])
      .toMatchObject({ outcome: 'unable', outcomeLabel: 'Could not review' })
    expect(reviewRows([{ role: 'audit', outcome: 'changes_requested', summary: 'The token is logged.' }])[0])
      .toMatchObject({ outcome: 'rejected', outcomeLabel: 'Rejected' })
    expect(reviewRows([{ role: 'review', outcome: null, summary: 'No verdict' }])[0]).toMatchObject({
      outcome: 'unknown', outcomeLabel: 'No verdict', lead: 'No verdict', more: false,
    })
  })

  it('collapses worker prose to one line, so a lead is never a paragraph fragment', () => {
    const [row] = reviewRows([{ role: 'review', outcome: 'passed', summary: '  Looks good!\n\nOne nit:   the name.  ' }])
    expect(row!.lead).toBe('Looks good!')
    expect(row!.summary).toBe('Looks good! One nit: the name.')
    expect(row!.more).toBe(true)
  })

  it('reads a pass that still needs a person as a warning, with the reason as its text', () => {
    const [row] = reviewRows([{
      role: 'audit', outcome: 'pass', summary: 'PASS.', human_required: true, human_reason: 'It rotates a signing key. A maintainer must confirm.',
    }])
    expect(row).toMatchObject({
      outcome: 'passed', outcomeLabel: 'Passed · needs a person', tone: 'warning',
      lead: 'It rotates a signing key.', summary: 'It rotates a signing key. A maintainer must confirm.', more: true,
    })
    // Only a pass can need a person; a rejection keeps its own verdict and text.
    expect(reviewRows([{ role: 'review', outcome: 'reject', summary: 'Broken.', human_required: true, human_reason: 'x' }])[0])
      .toMatchObject({ outcomeLabel: 'Rejected', tone: 'error', summary: 'Broken.' })
    expect(reviewRows([{ role: 'review', outcome: 'pass', summary: 'PASS.', human_required: true }])[0])
      .toMatchObject({ outcomeLabel: 'Passed · needs a person', summary: 'PASS.' })
  })

  it('is empty when the issue has no reviews', () => {
    expect(reviewRows(undefined)).toEqual([])
    expect(reviewRows([])).toEqual([])
  })
})

describe('reviewer control', () => {
  const people = [
    { login: 'maintainer', name: 'A Maintainer' },
    { login: 'second-pair', name: '' },
  ]

  it('offers every collaborator, with the display name when there is one', () => {
    expect(reviewerOptions(people)).toEqual([
      { value: 'maintainer', label: 'maintainer · A Maintainer' },
      { value: 'second-pair', label: 'second-pair' },
    ])
    expect(reviewerOptions(undefined)).toEqual([])
  })

  it('hints where each person was found', () => {
    expect(reviewerOptions([
      { login: 'a', name: '', source: 'collaborator' },
      { login: 'b', name: 'Bee', source: 'recent_author' },
      { login: 'c', name: '', source: 'configured' },
    ])).toEqual([
      { value: 'a', label: 'a', description: 'collaborator' },
      { value: 'b', label: 'b · Bee', description: 'recently merged work here' },
      { value: 'c', label: 'c', description: 'configured reviewer' },
    ])
  })

  it('starts from the configured human reviewers this repository actually offers', () => {
    expect(preselectedReviewers({ reviewers: people, default_reviewers: ['Maintainer', 'stranger'] })).toEqual(['Maintainer'])
    expect(preselectedReviewers({ reviewers: [], default_reviewers: ['maintainer'] })).toEqual([])
    expect(preselectedReviewers(null)).toEqual([])
  })

  it('sends the chosen logins as the decision, without duplicates or mentions', () => {
    expect(reviewerReason([' @maintainer ', 'second-pair', 'maintainer', ''])).toBe('maintainer, second-pair')
    expect(reviewerReason([])).toBe('')
  })

  it('says who was asked and how long ago', () => {
    expect(reviewRequestText({ logins: ['a', 'b'], at: 1790000000 }, 1790000720))
      .toBe('Review requested from @a, @b · 12 min ago')
  })

  it('drops the age when there is no clock yet, and says nothing when nobody was asked', () => {
    expect(reviewRequestText({ logins: ['a'], at: 1790000000 }, 0)).toBe('Review requested from @a')
    expect(reviewRequestText(null, 1790000000)).toBe('')
    expect(reviewRequestText({ logins: [], at: 1790000000 }, 1790000000)).toBe('')
  })
})

describe('summaryBlock', () => {
  const detail = (over = {}) => ({
    summary: { headline: 'Waiting for a person to approve the pull request', plain: 'The change is finished and needs a human approval before it can be merged. The audit says the deployment claims were not verified. Approve it on GitHub.' },
    explanation: 'The pull request needs a human review.',
    environment_blocker: null,
    ...over,
  })

  it('leads with the headline and the plain description, and keeps the rest for Details', () => {
    expect(summaryBlock(detail())).toEqual({
      headline: 'Waiting for a person to approve the pull request',
      plain: 'The change is finished and needs a human approval before it can be merged. The audit says the deployment claims were not verified. Approve it on GitHub.',
      details: 'The pull request needs a human review.',
    })
  })

  it('collects the environment blocker into Details, never into the description', () => {
    const block = summaryBlock(detail({ environment_blocker: 'Container exceeded its memory limit at /work/repository.' }))
    expect(block!.details).toBe('The pull request needs a human review. Container exceeded its memory limit at /work/repository.')
    expect(block!.plain).not.toContain('/work/repository')
  })

  it('falls back to the status line when Forge sent no summary', () => {
    expect(summaryBlock(detail({ summary: null }))).toEqual({
      headline: 'The pull request needs a human review.',
      plain: '',
      details: 'The pull request needs a human review.',
    })
    expect(summaryBlock(null)).toBeNull()
    expect(summaryBlock({ summary: null, explanation: '', environment_blocker: null })).toBeNull()
  })
})

describe('list filters in the URL', () => {
  it('reads only valid filters', () => {
    expect(filtersFromQuery({ show: 'completed', repo: '3', q: ' nav ', page: '2' })).toEqual({ show: 'completed', repo: '3', q: 'nav', page: 2 })
    expect(filtersFromQuery({ show: 'nonsense', repo: 'x', q: '', page: '0' })).toEqual({})
    expect(filtersFromQuery({})).toEqual({})
  })

  it('writes only what differs from the defaults', () => {
    expect(filterQuery({ show: 'open', repo: '', q: '', page: 1 })).toEqual({})
    expect(filterQuery({ show: 'completed', repo: '3', q: 'nav', page: 2 })).toEqual({ show: 'completed', repo: '3', q: 'nav', page: '2' })
  })

  it('round-trips', () => {
    const state = { show: 'attention', repo: '7', q: 'login', page: 4 }
    expect(filtersFromQuery(filterQuery(state))).toEqual(state)
  })
})

describe('waiting on other issues', () => {
  const blocked = { status: 'blocked' as const, explanation: 'Waiting for #90, #101.', dependencies: [85, 90, 101] }
  it('names the open prerequisites, from the explanation first, then the body', () => {
    expect(blockingIssues(blocked)).toEqual([90, 101])
    expect(blockingIssues({ ...blocked, explanation: '' })).toEqual([85, 90, 101])
    expect(blockingIssues({ ...blocked, status: 'queued' })).toEqual([])
  })
  it('is only opened, never asked to decide anything', () => {
    expect(rowAction({ status: 'blocked', actions: [], approval: null } as any, null)).toBe('Open')
  })
})

describe('attentionNote', () => {
  it('says a person is needed and points at the note, never "nothing needed"', () => {
    const summary = { headline: 'Held until a person verifies the release image', plain: '' }
    expect(attentionNote({ summary, explanation: null, environment_blocker: 'Verify the release image' } as any))
      .toBe('A person needs to verify this. See the note above.')
    expect(attentionNote({ summary, explanation: null, environment_blocker: null } as any))
      .toBe('A person needs to look at this. See the note above.')
    expect(attentionNote({ summary: null, explanation: null, environment_blocker: null } as any))
      .toBe('A person needs to look at this. Discuss the blocker to work out what.')
  })
})

describe('Forge fault report draft', () => {
  it('opens a reviewable GitHub draft with exact escaped text', () => {
    const title = 'Harness failed: A & B #12'
    const body = 'First line\nSecond line with ? & # characters'
    const url = new URL(forgeReportUrl({ status: 'draft', signature: 'safe-hash', title, body, affected: 2 })!)
    expect(url.origin + url.pathname).toBe('https://github.com/lucbocahut/forge/issues/new')
    expect(url.searchParams.get('title')).toBe(title)
    expect(url.searchParams.get('body')).toBe(body)
    expect(forgeReportUrl(null)).toBeUndefined()
  })
})
