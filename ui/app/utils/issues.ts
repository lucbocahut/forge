// Pure view logic and copy for the Issues list and the issue detail route.
// Status labels come from the shared server projection; the remaining maps
// name actions and supporting facts.
// Rules follow the operator API contracts §2 (GET /api/issues) and §3.2–3.3.

import { elapsed } from './factory'
import type { OperatorStatusSource } from './operatorStatus'
import { ago, clampTitle, reviewerMentions } from './format'

// `blocked`: waiting on other issues (its prerequisites) to close. It starts
// by itself when they do, so it never asks anything of the operator.
// `dropped`: closed on GitHub as not planned or as a duplicate. It is no
// longer needed, which is not the same as finished.
export type IssueStatus = 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'blocked' | 'completed' | 'dropped'
export type IssueAction = 'queue' | 'backlog' | 'pause' | 'resume' | 'retry' | 'recheck' | 'start_anyway' | 'assign_reviewer' | 'stop' | 'close'

// Which pull request this is to the issue: the one Forge is carrying now, an
// earlier Forge draft it moved on from, or someone else's.
type PullRequestKind = 'candidate' | 'forge_attempt' | 'external'
export interface PullRequestRef { number: number; title: string; kind: PullRequestKind }

interface IssueScope { id: number; name: string; complete: boolean; fresh: boolean; at: number | null; error: string | null }
// `at` (epoch seconds): when the decision was recorded. The server already
// drops receipts older than the issue's latest event.
export interface IssueActionStatus { id: string; key: string; status: 'pending' | 'applied' | 'rejected'; message: string | null; at?: number }

// The plain-language card Forge keeps for every issue: one headline sentence
// and at most three sentences, written for a reader who does not read code.
export interface IssueSummary { headline: string; plain: string; role?: string; at?: number }

export interface IssueEnvironmentSelection { mode: 'automatic' | 'standard' | 'full'; services: string[] }
export interface IssueEnvironment {
  choice: { tier: 'standard' | 'full'; services: string[]; reason: string; automatic: boolean; evidence?: string[]; evidence_head?: string | null } | null
  override: IssueEnvironmentSelection | null
  full_available: boolean
  editable: boolean
}

export interface IssuePrerequisite { reference: string; repository: string; number: number; kind: string | null; reason: string | null; complete: boolean; url: string }

export interface IssueRow extends OperatorStatusSource {
  steward_decision?: import('./steward').StewardDecisionMarker | null
  key: string; repo: string; repo_id: number; number: number; title: string
  content_hash: string; body_truncated: boolean; state: 'open' | 'closed'
  updated_at: string | null; labels: string[]; assignees: string[]
  status: IssueStatus; summary: IssueSummary | null; explanation: string
  ledger_state?: import('./factory').IssueState | null
  reason_code?: string | null
  stage_code?: string | null
  // The server sends the decisions this issue accepts; 'stop' is never one of
  // them (it targets the job, not the issue) and is offered separately.
  actions: Exclude<IssueAction, 'stop' | 'close'>[]
  // Whether Forge can drop this issue: close its pull request and close the
  // issue as not planned. Never one of `actions`.
  closable?: boolean
  delivery_approved?: boolean
  setup_needed: boolean; environment_blocker: string | null
  forge_problem_report?: { status: 'draft'; signature: string; title: string; body: string; affected: number } | null
  decision_request?: { question: string; options: string[]; recommendation: string; evidence: string; acknowledgment_scope?: string } | null
  approval?: { pr: number; reason: string } | null
  verification_request?: { what: string; why: string; created_at: number; gate_id?: string; can_confirm?: boolean; ci?: { name: string; app_id: number }[] } | null
  // Each reviewer's latest review on the candidate pull request as Forge last
  // read it, and when Forge last read GitHub for this issue.
  approvals?: IssueApproval[]
  last_checked?: number | null
  merge_policy?: 'auto' | 'human'
  merge_blocker?: { reason: string } | null
  environment?: IssueEnvironment
  pull_requests: PullRequestRef[]
  dependencies: number[]
  prerequisites?: IssuePrerequisite[]
  fresh: boolean; revision: string; setup_url: string; active: string | null
  // Sent on list rows and the detail alike; null until an attempt fails or a label holds it.
  attempts?: IssueAttempts | null
  held_by?: IssueLabelHold[] | null
}

// `at` is unix seconds; `why` says why a review did not count (null when it did).
interface IssueApproval {
  login: string; state: 'APPROVED' | 'CHANGES_REQUESTED' | 'DISMISSED'
  at: number | null; counted: boolean; why: string | null
}

// Who was asked for the human approval, and when. Set by the assign_reviewer
// decision; the approval itself still has to come through GitHub.
interface ReviewRequest { logins: string[]; at: number }
// Where Forge found this person: the repository's collaborators, the authors of
// recently merged work here, or the runtime's configured human reviewers.
type ReviewerSource = 'collaborator' | 'recent_author' | 'configured'
export interface Reviewer { login: string; name: string; source?: ReviewerSource }

interface IssueHistoryEvent { at: number; kind: string; reason?: string | null }
// `human_required`: the worker passed the change but says a person must still
// look (a secret, a migration, …); `human_reason` says why.
export interface IssueReview {
  role: 'review' | 'audit'; outcome: string | null; summary: string
  human_required?: boolean; human_reason?: string
}
interface IssueAttempts { failed: number; limit: number }
// A GitHub label that paused the issue, who applied it and when (ISO time).
interface IssueLabelHold { label: string; login: string | null; at: string }
export interface IssueDetail extends IssueRow {
  delivery_id?: string | null
  body: string; history: IssueHistoryEvent[]; reviews: IssueReview[]
  // Detail only: the list view carries neither the collaborators nor the request.
  reviewers: Reviewer[]; default_reviewers: string[]; reviewers_requested: ReviewRequest | null
}

export interface IssueListResponse {
  ok: true; controls_token: string; issues: IssueRow[]; total: number; page: number; pages: number
  repositories: IssueScope[]; complete: boolean; actions: IssueActionStatus[]
}
export interface IssueDetailResponse {
  cursor?: string; jobs?: import('./factory').JobView[]
  factory_issue?: import('./factory').LedgerIssueView | null
  deployments?: import('./factory').FactoryView['deployments']
  ok: true; controls_token: string; issue: IssueDetail; repositories: IssueScope[]; actions: IssueActionStatus[]
}

// The open prerequisites a blocked issue waits on. The server names them in
// the explanation ("Waiting for #90, #101."); the dependencies the body lists
// are the fallback. Empty for any other status.
export function blockingIssues(row: Pick<IssueRow, 'status' | 'explanation' | 'dependencies'>): number[] {
  if (row.status !== 'blocked') return []
  const named = [...(row.explanation ?? '').matchAll(/#(\d+)/g)].map(match => Number(match[1]))
  const numbers = named.length ? named : (row.dependencies ?? [])
  return [...new Set(numbers.filter(number => Number.isSafeInteger(number) && number > 0))]
}

// The main label is shared with Activity; dependency details stay in the reason.
export function statusLabel(row: Pick<IssueRow, 'operator_status'>): string {
  return row.operator_status.label
}

export const ACTION_LABELS: Record<IssueAction, string> = {
  queue: 'Queue for Forge',
  backlog: 'Return to backlog',
  pause: 'Pause',
  resume: 'Release hold',
  retry: 'Retry attempt',
  recheck: 'Recheck readiness',
  start_anyway: 'Start anyway…',
  assign_reviewer: 'Assign reviewer',
  stop: 'Stop attempt',
  close: 'Close issue',
}

export function actionLabel(action: IssueAction, issue?: Pick<IssueRow, 'reason_code'>): string {
  return action === 'recheck' && issue?.reason_code === 'external_change' ? 'Reload and continue' : ACTION_LABELS[action]
}

export function actionEffect(action: IssueAction, issue?: Pick<IssueRow, 'reason_code'>): string {
  return action === 'recheck' && issue?.reason_code === 'external_change'
    ? 'Forge reloads the issue and pull request. If an open pull request has changed, Forge rebuilds it and repeats review and audit. Other holds remain. Reopen a closed pull request, mark a draft ready, or restore its target branch on GitHub before continuing.'
    : ACTION_EFFECTS[action]
}

// What a decision's confirm button says while Forge is answering it.
export const ACTION_PENDING_LABELS: Record<IssueAction, string> = {
  queue: 'Queuing…',
  backlog: 'Returning…',
  pause: 'Pausing…',
  resume: 'Releasing…',
  retry: 'Retrying…',
  recheck: 'Rechecking…',
  start_anyway: 'Clearing dependency wait…',
  assign_reviewer: 'Assigning…',
  stop: 'Stopping…',
  close: 'Closing…',
}

export const ACTION_EFFECTS: Record<IssueAction, string> = {
  queue: 'Forge will check repository setup, dependencies and existing work before starting when capacity is available.',
  backlog: 'This removes the issue from Forge’s queue. The GitHub issue stays open.',
  pause: 'Forge will leave this issue on hold until you choose to resume it.',
  resume: 'This releases the deliberate hold. Confirm that the prerequisite work is complete. Forge will check dependencies and existing work before continuing.',
  retry: 'This grants a new three-attempt budget and returns the issue to Build. Existing history is preserved.',
  recheck: 'Forge will recheck readiness and resume the held stage. The existing pull request and failure budget are preserved. Confirm that the reported blocker has been addressed.',
  start_anyway: 'This clears the prerequisites reported by the worker for the next attempt. Explain why work can proceed. Dependencies written explicitly in the GitHub issue still apply; edit its description to change them.',
  assign_reviewer: 'Forge asks GitHub for a review from these people and assigns them to the issue. The approval still has to come from a reviewer with write access who did not write the code.',
  stop: 'This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.',
  close: 'Forge stops any running work, closes its pull request and closes the issue on GitHub as not planned, with your reason as a comment.',
}

export const EVENT_LABELS: Record<string, string> = {
  discovered: 'Added to Forge',
  held: 'Put on hold',
  protection_paused: 'Repository setup paused work',
  protection_restored: 'Repository setup verified',
  human_retry: 'Another attempt approved',
  draft_resumed: 'Continuing an earlier draft',
  decision_requested: 'Waiting for your decision',
  verification_requested: 'External verification requested',
  verification_completed: 'Verification confirmed',
  decision_answered: 'Your answer recorded',
  environment_selected: 'Worker environment selected',
  environment_override: 'Issue environment preference saved',
  services_retry: 'Retrying with database services',
  operator_queue: 'Queued for Forge',
  operator_backlog: 'Returned to backlog',
  operator_pause: 'Paused by an operator',
  operator_resume: 'Hold released',
  operator_recheck: 'Repository setup checked',
  dependencies_declared: 'Worker reported prerequisite issues',
  operator_start_anyway: 'Worker dependency wait cleared by an operator',
  reviewer_requested: 'Review requested',
  operator_conflict: 'Decision needs another review',
  operator_synced: 'GitHub labels confirmed',
  reserved: 'Attempt prepared',
  merged: 'Pull request merged',
  cancelled: 'Attempt stopped',
  operator_close: 'Closed as not planned',
}

export const SHOW_OPTIONS: { value: string; label: string }[] = [
  { value: 'open', label: 'Open issues' },
  { value: 'attention', label: 'Needs attention' },
  { value: 'backlog', label: 'Backlog' },
  { value: 'queued', label: 'Queued' },
  { value: 'working', label: 'Working' },
  { value: 'waiting', label: 'Waiting' },
  { value: 'blocked', label: 'Waiting on other issues' },
  { value: 'completed', label: 'Completed' },
  { value: 'dropped', label: 'No longer needed' },
]

// Decisions that change what Forge does next are recorded with the operator's
// own words; the reversible ones default to "<label> confirmed by operator".
const REASON_REQUIRED: IssueAction[] = ['pause', 'resume', 'retry', 'start_anyway', 'stop', 'close']

export function decisionNeedsReason(action: IssueAction): boolean {
  return REASON_REQUIRED.includes(action)
}

export function defaultReason(action: IssueAction): string {
  return `${ACTION_LABELS[action]} confirmed by operator`
}

// What the issue page leads with: the headline, then the description, then the
// action bar. Everything the worker wrote stays available under "Details", so
// the reader never has to open it to learn where the issue stands or why.
interface SummaryBlock { headline: string; plain: string; details: string }

export function summaryBlock(
  issue: Pick<IssueDetail, 'summary' | 'explanation' | 'environment_blocker'> | null | undefined,
): SummaryBlock | null {
  if (!issue) return null
  const details = [issue.explanation, issue.environment_blocker].map(part => (part ?? '').trim()).filter(Boolean).join(' ')
  const headline = (issue.summary?.headline ?? '').trim() || leadSentence(details)
  const plain = (issue.summary?.plain ?? '').trim() || (headline === details ? '' : details)
  if (!headline && !plain) return null
  return { headline, plain, details }
}

// The worker's technical report, for the toggle under the bubble: only when
// it says more than the plain words the bubble already shows.
export function technicalDetails(
  issue: Pick<IssueDetail, 'summary' | 'explanation' | 'environment_blocker'> | null | undefined,
  shown: string[],
): string {
  const details = summaryBlock(issue)?.details ?? ''
  const said = shown.map(line => line.trim().toLowerCase())
  return details && !said.includes(details.toLowerCase()) ? details : ''
}

// A terse note as a sentence: a capital first letter and a full stop.
export function sentence(text: string): string {
  const trimmed = text.trim()
  if (!trimmed) return ''
  const capital = trimmed[0]!.toUpperCase() + trimmed.slice(1)
  return /[.!?…]$/.test(capital) ? capital : `${capital}.`
}

// An issue that needs a person but offers no button: say so, and point at
// the note that explains what (the summary above the action bar), instead
// of claiming nothing is needed.
export function attentionNote(issue: Pick<IssueDetail, 'summary' | 'explanation' | 'environment_blocker'> | null | undefined): string {
  const what = issue?.environment_blocker ? 'verify this' : 'look at this'
  return summaryBlock(issue) ? `A person needs to ${what}. See the note above.` : `A person needs to ${what}. Discuss the blocker to work out what.`
}

// A review summary is worker prose: the detail page shows its lead sentence
// clamped to two lines, and reveals the rest on demand. The row model is pure
// so the copy the operator reads is decided here, not in the template.
type ReviewOutcome = 'passed' | 'rejected' | 'unable' | 'unknown'

interface ReviewRow {
  role: IssueReview['role']
  label: string
  // The verdict this reviewer returned, never the issue's own status: a passed
  // review on an issue that needs attention is still a passed review.
  outcome: ReviewOutcome
  outcomeLabel: string
  // A pass that still needs a person reads as a warning, not a green light.
  tone: 'success' | 'error' | 'warning' | 'neutral'
  lead: string
  summary: string
  // True when the lead leaves something out, so the row offers "More".
  more: boolean
}

const REVIEW_LABELS: Record<IssueReview['role'], string> = { review: 'Review', audit: 'Security review' }
// Worker verdicts ('pass'/'reject'/'unable'), plus the older published names.
const REVIEW_OUTCOMES: Record<string, ReviewOutcome> = {
  pass: 'passed', passed: 'passed', approved: 'passed',
  reject: 'rejected', rejected: 'rejected', changes_requested: 'rejected',
  unable: 'unable',
}
const REVIEW_OUTCOME_LABELS: Record<ReviewOutcome, string> = {
  passed: 'Passed', rejected: 'Rejected', unable: 'Could not review', unknown: 'No verdict',
}
const REVIEW_TONES: Record<ReviewOutcome, ReviewRow['tone']> = {
  passed: 'success', rejected: 'error', unable: 'warning', unknown: 'neutral',
}

function leadSentence(text: string): string {
  const end = text.search(/[.!?](\s|$)/)
  return end === -1 ? text : text.slice(0, end + 1)
}

function reviewRow(review: IssueReview): ReviewRow {
  const outcome = REVIEW_OUTCOMES[(review.outcome ?? '').toLowerCase()] ?? 'unknown'
  // The summary of such a pass is often just "PASS."; the reason is the news.
  const needsPerson = outcome === 'passed' && !!review.human_required
  const text = (needsPerson && (review.human_reason ?? '').trim()) || review.summary || ''
  const summary = text.replace(/\s+/g, ' ').trim()
  const lead = leadSentence(summary)
  return {
    role: review.role,
    label: REVIEW_LABELS[review.role] ?? review.role,
    outcome,
    outcomeLabel: needsPerson ? 'Passed · needs a person' : REVIEW_OUTCOME_LABELS[outcome],
    tone: needsPerson ? 'warning' : REVIEW_TONES[outcome],
    lead,
    summary,
    more: summary !== lead,
  }
}

export function reviewRows(reviews: IssueReview[] | null | undefined): ReviewRow[] {
  return (reviews ?? []).map(reviewRow)
}

// The reviewer control on an approval_needed issue. Everything the operator
// reads or sends is decided here: the select's options, what it starts with,
// the decision text that becomes `reason`, and the line the page shows once
// GitHub has been asked.
interface ReviewerOption { value: string; label: string; description?: string }

const REVIEWER_SOURCES: Record<ReviewerSource, string> = {
  collaborator: 'collaborator',
  recent_author: 'recently merged work here',
  configured: 'configured reviewer',
}

// Only people Forge found for this repository: there is no free-text entry,
// so a typo can never ask a stranger for the review.
export function reviewerOptions(reviewers: Reviewer[] | null | undefined): ReviewerOption[] {
  return (reviewers ?? [])
    .filter(person => !!person?.login)
    .map((person) => {
      const option: ReviewerOption = { value: person.login, label: person.name ? `${person.login} · ${person.name}` : person.login }
      const source = person.source ? REVIEWER_SOURCES[person.source] : undefined
      if (source) option.description = source
      return option
    })
}

// Pre-selected: the people the runtime already trusts to approve, kept to the
// ones this repository actually offers so the select never holds a stranger.
export function preselectedReviewers(
  detail: Pick<IssueDetail, 'reviewers' | 'default_reviewers'> | null | undefined,
): string[] {
  const known = new Set((detail?.reviewers ?? []).map(person => person.login.toLowerCase()))
  return (detail?.default_reviewers ?? []).filter(login => known.has(login.toLowerCase()))
}

export function reviewerReason(logins: string[]): string {
  const cleaned = logins.map(login => login.trim().replace(/^@+/, '')).filter(Boolean)
  return [...new Set(cleaned)].join(', ')
}

export function reviewRequestText(request: ReviewRequest | null | undefined, now: number): string {
  if (!request?.logins?.length) return ''
  const line = `Review requested from ${reviewerMentions(request.logins)}`
  const age = now - (request.at ?? 0)
  return request.at && age >= 0 ? `${line} · ${elapsed(age)} ago` : line
}

// One line per review Forge found, in words and an icon, never colour alone:
// "@octo-operator approved · 16:03 · counted". `time` formats `at`; pass nothing
// before mount, when the operator's time zone is not known yet.
interface ApprovalLine { login: string; text: string; counted: boolean; icon: string }
export function approvalLines(approvals: IssueApproval[] | null | undefined, time?: (at: number) => string): ApprovalLine[] {
  return (approvals ?? []).map((review) => {
    const who = '@' + review.login
    const verb = review.state === 'APPROVED' ? 'approved'
      : review.state === 'CHANGES_REQUESTED' ? 'requested changes' : 'approval dismissed'
    const parts = [`${who} ${verb}`]
    if (review.at && time) parts.push(time(review.at))
    if (review.counted) parts.push('counted')
    else if (review.state === 'CHANGES_REQUESTED') parts.push('blocks the merge')
    else if (review.state === 'DISMISSED') parts.push('not counted')
    else parts.push(review.why ? `not counted: ${review.why}` : 'not counted')
    return { login: review.login, text: parts.join(' · '), counted: review.counted,
      icon: review.counted ? 'i-lucide-circle-check' : 'i-lucide-circle-x' }
  })
}

// "Checked on GitHub 20 s ago": seconds matter here, because the operator is
// waiting for Forge to see an approval they just gave.
export function checkedText(lastChecked: number | null | undefined, now: number): string {
  if (!lastChecked) return 'Not checked on GitHub yet'
  if (!now) return 'Checked on GitHub'
  return `Checked on GitHub ${elapsed(now - lastChecked)} ago`
}

// The pull request a review is requested on: the candidate Forge carries.
export function reviewPull(pulls: PullRequestRef[] | null | undefined): PullRequestRef | null {
  return (pulls ?? []).find(pull => pull.kind === 'candidate') ?? null
}

const PULL_PREFIXES: Record<PullRequestKind, string> = {
  candidate: 'PR', forge_attempt: 'Earlier Forge draft', external: 'Other PR',
}
// The issue panel reads in full words; compact cards keep the short labels.
const PULL_NAMES: Record<PullRequestKind, string> = {
  candidate: 'Pull request', forge_attempt: 'Earlier Forge draft', external: 'Other pull request',
}

export function pullLabel(pull: PullRequestRef): string {
  return `${PULL_PREFIXES[pull.kind] ?? 'PR'} #${pull.number}`
}

export function pullName(pull: PullRequestRef): string {
  return `${PULL_NAMES[pull.kind] ?? 'Pull request'} #${pull.number}`
}

export function attemptsText(attempts: IssueAttempts | null | undefined): string {
  if (!attempts || !(attempts.failed > 0)) return ''
  return `${attempts.failed} of ${attempts.limit} attempts failed`
}

// "Labelled forge:hold by @maintainer on Sep 21", one line per label. The
// date is the UTC day, so the server and the browser render the same text.
export function heldByLines(holds: IssueLabelHold[] | null | undefined): string[] {
  return (holds ?? []).filter(hold => !!hold?.label).map((hold) => {
    const by = hold.login ? ` by @${hold.login}` : ''
    const at = new Date(hold.at)
    const on = Number.isNaN(at.getTime())
      ? ''
      : ` on ${at.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })}`
    return `Labelled ${hold.label}${by}${on}`
  })
}

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] ?? kind.replace(/_/g, ' ')
}

export function issuePath(key: string, job?: string | null, tab?: 'details' | 'discussion' | null): string {
  const params = [job ? `job=${encodeURIComponent(job)}` : '', tab === 'discussion' ? 'tab=discussion' : ''].filter(Boolean)
  return `/issues/${encodeURIComponent(key)}${params.length ? `?${params.join('&')}` : ''}`
}

// The list's filters live in the URL (`?show=&repo=&q=&page=`), so a filtered
// view reloads and shares as itself. Defaults are left out.
interface FilterState { show: string; repo: string; q: string; page: number }

export function filtersFromQuery(query: Record<string, unknown>): Partial<FilterState> {
  const one = (value: unknown) => { const found = Array.isArray(value) ? value[0] : value; return typeof found === 'string' && found ? found : undefined }
  const out: Partial<FilterState> = {}
  const show = one(query.show)
  if (show && SHOW_OPTIONS.some(option => option.value === show)) out.show = show
  const repo = one(query.repo)
  if (repo && /^\d+$/.test(repo)) out.repo = repo
  const q = one(query.q)?.trim()
  if (q) out.q = q
  const page = Number(one(query.page))
  if (Number.isSafeInteger(page) && page > 1) out.page = page
  return out
}

export function filterQuery({ show, repo, q, page }: FilterState): Record<string, string> {
  const out: Record<string, string> = {}
  if (show !== 'open') out.show = show
  if (repo) out.repo = repo
  if (q) out.q = q
  if (page > 1) out.page = String(page)
  return out
}

interface ListFilters { state: string; repoId: number | null; q: string; page: number }

export function listParams({ state, repoId, q, page }: ListFilters): string {
  const params = new URLSearchParams()
  params.set('state', state)
  params.set('page', String(page))
  if (repoId !== null && repoId !== undefined) params.set('repo_id', String(repoId))
  const search = (q ?? '').trim()
  if (search) params.set('q', search)
  return params.toString()
}

// The last decision recorded for a key is the one being applied: the server
// appends a new IssueActionStatus per request and keeps the previous ones.
export function pendingAction(actions: IssueActionStatus[] | undefined, key: string): IssueActionStatus | null {
  const forKey = (actions ?? []).filter(action => action.key === key)
  const last = forKey[forKey.length - 1]
  return last && last.status === 'pending' ? last : null
}

export function lastAction(actions: IssueActionStatus[] | undefined, key: string): IssueActionStatus | null {
  const forKey = (actions ?? []).filter(action => action.key === key)
  return forKey[forKey.length - 1] ?? null
}

// The one button on an issue card. It always opens the issue — decisions are
// confirmed there — but it is named after the decision the issue is waiting
// for, so the list says what opening it is for. Returning to the backlog or
// pausing are never the point of opening a queued issue, so they are not named.
const PRIMARY_DECISIONS: IssueAction[] = ['assign_reviewer', 'retry', 'recheck', 'resume', 'queue']

interface RowAction { label: string; decision: boolean }

export function rowPrimary(
  row: Pick<IssueRow, 'status' | 'actions' | 'approval' | 'reason_code'>, pending: IssueActionStatus | null | undefined,
): RowAction {
  if (pending) return { label: 'Applying…', decision: false }
  if (row.approval) return { label: 'Approve…', decision: true }
  const decision = PRIMARY_DECISIONS.find(action => (row.actions ?? []).includes(action as IssueRow['actions'][number]))
  if (decision) return { label: actionLabel(decision, row), decision: true }
  if (row.status === 'working') return { label: 'View progress', decision: false }
  if (row.status === 'attention') return { label: 'Review', decision: false }
  return { label: 'Open', decision: false }
}

export function rowAction(row: Pick<IssueRow, 'status' | 'actions' | 'approval' | 'reason_code'>, pending: IssueActionStatus | null | undefined): string {
  return rowPrimary(row, pending).label
}

// The story an issue card tells: Forge's headline (or the older explanation
// line), then the plain-language sentences behind it when they add anything.
interface RowStory { headline: string; plain: string }

export function rowStory(row: Pick<IssueRow, 'summary' | 'explanation'>): RowStory {
  const explanation = (row.explanation ?? '').trim()
  const headline = (row.summary?.headline ?? '').trim() || explanation
  const plain = (row.summary?.plain ?? '').trim()
  return { headline, plain: plain && plain !== headline ? plain : '' }
}

// "Updated 3 h ago" from GitHub's ISO time; nothing when it is missing or
// unreadable, so a card never claims an age it does not know.
export function activityAge(updatedAt: string | null | undefined, now: number): string {
  if (!updatedAt) return ''
  const at = Date.parse(updatedAt)
  if (Number.isNaN(at)) return ''
  return `Updated ${ago(now - at / 1000)}`
}

// The compact facts under an issue card's story, in reading order, and only
// the ones the list row actually carries.
interface RowFact { key: string; text: string }

export function rowFacts(
  row: Pick<IssueRow, 'pull_requests' | 'attempts' | 'dependencies' | 'held_by' | 'updated_at'>, now: number,
): RowFact[] {
  const facts: RowFact[] = (row.pull_requests ?? []).map(pull => ({ key: `pr-${pull.number}`, text: pullLabel(pull) }))
  const attempts = attemptsText(row.attempts)
  if (attempts) facts.push({ key: 'attempts', text: attempts })
  const dependencies = (row.dependencies ?? []).filter(number => Number.isFinite(number))
  if (dependencies.length) facts.push({ key: 'dependencies', text: `Depends on ${dependencies.map(number => `#${number}`).join(', ')}` })
  const holds = [...new Set((row.held_by ?? []).map(hold => hold?.label).filter(Boolean))]
  if (holds.length) facts.push({ key: 'hold', text: `On hold: ${holds.join(', ')}` })
  const age = activityAge(row.updated_at, now)
  if (age) facts.push({ key: 'age', text: age })
  return facts
}

export function freshnessText(list: Pick<IssueListResponse, 'repositories' | 'complete'>): string {
  const failing = (list.repositories ?? []).find(scope => scope.error)
  if (failing?.error) return failing.error
  if (!(list.repositories ?? []).length) return ''
  if (list.complete) return 'Up to date · Viewing issues never queues them for work.'
  return 'Showing received issues. The GitHub inventory is still incomplete or needs refreshing.'
}

interface EmptyState { title: string; text: string; link?: { to: string; label: string } }

export function emptyState(list: Pick<IssueListResponse, 'issues' | 'repositories' | 'complete'>): EmptyState | null {
  if ((list.issues ?? []).length) return null
  if (!(list.repositories ?? []).length) {
    return {
      title: 'Choose where Forge should work',
      text: 'Add and enable a repository to see its issues here.',
      link: { to: '/settings/repositories?add=1', label: 'Add repository →' },
    }
  }
  if (!list.complete) {
    return { title: 'Loading the issue list', text: 'Previously received issues remain visible while Forge checks GitHub.' }
  }
  return { title: 'No issues in this view', text: 'Try another filter.' }
}

export function countText(list: Pick<IssueListResponse, 'total' | 'page' | 'pages' | 'complete'>): string {
  const issues = `${list.total} ${list.total === 1 ? 'issue' : 'issues'}${list.complete ? '' : ' received'}`
  return `${issues} · Page ${list.page} of ${Math.max(list.pages, 1)}`
}

export function forgeReportUrl(report: IssueRow['forge_problem_report']): string | undefined {
  if (!report || report.status !== 'draft') return undefined
  return 'https://github.com/lucbocahut/forge/issues/new?' + new URLSearchParams({ title: report.title, body: report.body })
}
