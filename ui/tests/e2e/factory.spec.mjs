import assert from 'node:assert/strict'
import { chooseMore } from './more-menu.mjs'

const ORPHAN_RECOVERY_JOB = '33333333-3333-4333-8333-333333333333'
const GITHUB_WARNING = 'GitHub observation or publication is unconfirmed'
// The detail panel is the slideover on the right; a stack deck and a
// confirmation are ordinary modals, so the three never collide in a locator.
const PANEL = '[role="dialog"][data-side="right"]'
const MODAL = '[role="dialog"]:not([data-side="right"])'
const CONFIRM = '[role="dialog"]:has(#confirm-reason)'
// The issue the mock serves for every /api/issues?issue= request.
const DETAIL_TITLE = 'Make course navigation easier'

// Counts how many times a string appears in the rendered (visible) page text.
function occurrences(page, text) {
  return page.evaluate((needle) => document.body.innerText.split(needle).length - 1, text)
}

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

// Opens the "need you" list from the readout and waits for it to hold still:
// the dialog animates in with a scale, so a card measured too early is
// smaller than it renders.
async function openNeedsYou(page) {
  // The count is server-rendered; it opens the list once the page hydrated.
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  await page.locator('#readout [data-readout="attention"] button').click()
  const dialog = page.locator(MODAL)
  await dialog.locator('.issue-card').first().waitFor({ timeout: 10000 })
  await page.waitForFunction((selector) => {
    const dialog = document.querySelector(selector)
    if (!dialog || dialog.getAnimations().some((animation) => animation.playState === 'running')) return false
    const card = dialog.querySelector('.issue-card')
    return !!card && Math.abs(card.getBoundingClientRect().height - card.offsetHeight) < 1
  }, MODAL, { timeout: 10000 })
  return dialog
}

// The panel's own open/close, used from every section below.
async function openPanel(page, card) {
  await card.click()
  const panel = page.locator(PANEL)
  await panel.waitFor({ timeout: 10000 })
  return panel
}

async function closePanel(page) {
  await page.keyboard.press('Escape')
  await page.locator(PANEL).waitFor({ state: 'hidden', timeout: 10000 })
}

function query(page, name) {
  return new URL(page.url()).searchParams.get(name)
}


// End-to-end behaviour of the Activity (Factory) page: the factory line and
// its one-line readout, cartoon cards only where the operator acts, one
// detail route per card, a single message block above the strip, the stop
// confirmation round-trip, stage moves and the reset fence.
export default async function (page, { base, scenario, requests, observeIssue }) {
  await page.goto(base + '/')
  await page.locator('#factory-line').waitFor()
  // The strip does the stat tiles' and stage columns' job now.
  assert.equal(await page.locator('[data-breakdown]').count(), 0, 'the five stat tiles are gone')
  assert.equal(await page.locator('#columns, #stage-build, #stage-review, #stage-audit').count(), 0, 'the stage columns are gone')
  assert.equal(await page.locator('#waiting, #finishing').count(), 0, 'waiting and finishing live in the strip and on Issues')
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Activity overflows at ${width}`)
    await page.screenshot({ path: `/tmp/forge-activity-${width}.png`, fullPage: true })
  }
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.locator('#readout').waitFor()

  // The readout under the strip: live counts, and "Needs you" is the strip's
  // own crate count (its headline reads "2 need you").
  const readout = page.locator('#readout')
  const value = async (id) => (await readout.locator(`[data-readout="${id}"]`).innerText()).replace(/\s+/g, ' ').trim()
  // The fixture's observation is days old, so no worker counts as working.
  assert.equal(await value('working'), 'Working 0 of 4 · 1 slot in use')
  assert.equal(await value('queued'), 'Queued 5')
  assert.equal(await value('merging'), 'Waiting to merge 1')
  assert.equal(await value('attention'), 'Needs you 2')
  assert.equal(await value('merged'), 'Merged today 0')
  assert.match(await page.locator('.factory-line canvas[role="img"]').getAttribute('aria-label'), /^2 need you/)
  const needsYou = readout.locator('[data-readout="attention"] button')
  assert.equal(await needsYou.getAttribute('aria-haspopup'), 'dialog')
  assert.equal(await needsYou.getAttribute('aria-label'), 'Show the 2 issues that need you')
  assert.match(await needsYou.getAttribute('class'), /text-amber-200/)
  assert.equal(await readout.locator('[data-readout="working"] a').getAttribute('href'), '/issues?show=working')
  const readoutBox = await readout.boundingBox()
  const lastItem = await readout.locator('[data-readout="merged"]').boundingBox()
  assert.ok(readoutBox.x + readoutBox.width - (lastItem.x + lastItem.width) < 2, 'the readout is right-aligned on desktop')

  // The Needs attention section is gone: the strip's crate (and this count)
  // open the issues that need the operator as a list, each with its cube:
  // the failed issue is lit amber, worried and scorched by its three attempts.
  assert.equal(await page.locator('#needs-attention, .issue-stack').count(), 0, 'the Needs attention section is gone')
  let needs = await openNeedsYou(page)
  const attentionCards = needs.locator('.issue-card')
  assert.equal(await attentionCards.count(), 2)
  const failed = needs.locator('[data-key="1:604"]')
  const cube = failed.locator('canvas.cube-portrait')
  assert.equal(await cube.getAttribute('aria-hidden'), 'true')
  assert.match(await cube.getAttribute('data-look'), /worried .*beacon scorch-3/)
  assert.equal((await failed.locator('[data-status-chip]').innerText()).trim(), 'Needs attention')
  assert.equal((await failed.locator('[data-fact="attempts"]').innerText()).trim(), '3 of 3 attempts failed')
  assert.equal(await failed.locator('[data-fact="attempts"] [data-heart="lost"]').count(), 3)
  // The cube is drawn once it is on screen.
  assert.ok(await cube.evaluate(canvas => {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data
    return data.some((channel, index) => index % 4 === 3 && channel > 0)
  }), 'the portrait has been drawn')

  // The worker-output popover is gone; logs live in the detail view.
  // A worker busy on an issue is shown by the strip, never repeated as a
  // status line above it (the default operator status is "Building issue #601").
  assert.equal(await page.getByRole('link', { name: 'View issue' }).count(), 0)
  assert.equal(await occurrences(page, 'Building issue #601'), 0)
  assert.equal(await page.getByRole('button', { name: 'Output' }).count(), 0)
  assert.equal(await page.getByRole('link', { name: 'Output' }).count(), 0)

  // The card is the control; its one action sits beside it. A modified
  // click opens the issue's own page in a new tab, not the panel.
  const card = needs.locator('[data-key="1:604"]')
  assert.equal(await card.getAttribute('role'), 'link')
  assert.equal(await card.getAttribute('aria-label'), 'Open XRPL-Commons/academy #604')
  assert.equal(await card.getByRole('link', { name: 'Retry…' }).count(), 1)
  const [tab] = await Promise.all([page.context().waitForEvent('page'), card.click({ modifiers: ['ControlOrMeta'] })])
  await tab.waitForLoadState()
  assert.equal(new URL(tab.url()).pathname, '/issues/1%3A604')
  await tab.close()
  assert.equal(query(page, 'issue'), null, 'a modified click leaves the panel closed')
  // A plain click replaces the list with the issue's detail panel.
  let panel = await openPanel(page, card)
  await needs.waitFor({ state: 'hidden', timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  assert.equal(query(page, 'issue'), '1:604', 'the open panel is carried by the URL')
  assert.equal(await panel.locator('#issue-full-page').getAttribute('href'), '/issues/1%3A604')
  // Escape closes it and leaves the URL clean.
  await closePanel(page)
  assert.equal(new URL(page.url()).search, '', `the closed panel left the URL dirty: ${page.url()}`)
  // No card left on the page carries a button: the list's actions close with it.
  assert.equal(await page.locator('.issue-card button').count(), 0, 'no card renders a button')

  // …so a reload of that URL opens the same panel, and the board keeps
  // polling behind it.
  await page.goto(base + '/?issue=1%3A601')
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  await closePanel(page)

  // The standalone page is unchanged: the same detail, no overlay.
  await page.goto(base + '/issues/1%3A601')
  await waitForText(page, DETAIL_TITLE)
  assert.equal(await page.locator('[role="dialog"]').count(), 0, 'the standalone page renders no overlay')

  // An internal run opens the same panel at its own job, and its Reset moved
  // in with it: the board's confirmation, the board's payload.
  // Investigations are drawn nowhere else, so they keep one collapsed line.
  // The issue envelope must identify the recovery job's repository and issue.
  await observeIssue('1:610', { key: '3:900', repo_id: 3, repo: 'lucbocahut/forge',
    number: 900, url: 'https://github.com/lucbocahut/forge/issues/900', active: null })
  await page.goto(base + '/')
  await waitForText(page, 'Investigations and cleanup · 2')
  assert.equal(await page.locator('#runners .issue-card').count(), 0, 'investigations stay folded until asked for')
  await page.locator('#runners summary').click()
  const internal = page.locator('#stage-recovery [data-key="3:900"]')
  await internal.waitFor({ timeout: 10000 })
  assert.match(await internal.locator('canvas').getAttribute('data-look'), /^queued /)
  panel = await openPanel(page, internal)
  assert.equal(query(page, 'issue'), '3:900')
  assert.equal(query(page, 'job'), ORPHAN_RECOVERY_JOB, 'an internal card opens at its own run')
  await chooseMore(page, panel, 'Reset runner…')
  const resetDialog = page.locator(CONFIRM)
  await resetDialog.waitFor({ timeout: 10000 })
  const confirmReset = resetDialog.getByRole('button', { name: 'Reset runner' })
  assert.equal(await confirmReset.isDisabled(), true, 'the confirm button must require a reason')
  await resetDialog.getByRole('textbox', { name: 'Decision or reason' }).fill('Runner wedged')
  await confirmReset.click()
  await resetDialog.waitFor({ state: 'hidden', timeout: 10000 })
  const reset = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(reset.length, 1)
  assert.deepEqual(reset[0].body, { action: 'reset', target: ORPHAN_RECOVERY_JOB, reason: 'Runner wedged' })
  await closePanel(page)

  // A supervisor observation error is one banner that absorbs the operator
  // status line: each sentence is rendered exactly once, and its button
  // points at the remediation for that phase.
  await scenario('warning')
  await waitForText(page, GITHUB_WARNING)
  assert.equal(await occurrences(page, GITHUB_WARNING), 1)
  assert.equal(await occurrences(page, 'Forge needs attention'), 1)
  const warningAction = page.getByRole('link', { name: 'Open GitHub access' })
  assert.equal(await warningAction.count(), 1)
  assert.equal(await warningAction.getAttribute('href'), '/settings/github')

  // Stop lives in the panel's action bar now: it asks for a decision, then
  // posts exactly {action, target, reason} against the issue's own attempt.
  await scenario('default')
  // The running build is drawn on the strip, so its panel opens from the URL.
  await page.goto(base + '/?issue=1%3A601')
  panel = page.locator(PANEL)
  await panel.waitFor({ timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  await chooseMore(page, panel, 'Stop attempt…')
  const stopDialog = page.locator(CONFIRM)
  await stopDialog.waitFor({ timeout: 10000 })
  await waitForText(page, 'Stop attempt?')
  const confirmStop = stopDialog.getByRole('button', { name: 'Stop attempt' })
  assert.equal(await confirmStop.isDisabled(), true, 'the confirm button must require a reason')
  await stopDialog.getByRole('textbox', { name: 'What changed?' }).fill('Stuck for an hour')
  assert.equal(await confirmStop.isDisabled(), false)
  await confirmStop.click()
  await waitForText(page, 'Decision submitted. Waiting for confirmation.')

  const posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(posted.length, 1)
  assert.deepEqual(posted[0].body, { action: 'stop', target: 'attempt', reason: 'Stuck for an hour' })
  await closePanel(page)

  // A rejected operator request comes back through the next poll as a banner.
  await scenario('stop-rejected')
  await waitForText(page, 'Action could not be applied: Worker already stopped')
  assert.ok(await page.getByText('Action could not be applied: Worker already stopped').isVisible())

  // An issue that changes stage moves along the strip and is announced.
  await scenario('moved')
  await page.waitForFunction(
    () => document.querySelector('p.sr-only')?.textContent.trim() === 'Issue 601 moved to Review',
    null, { timeout: 10000 },
  )

  // A reset fence replaces the last known state on the next poll alone: one
  // banner, no strip, no readout, no cards — and it lifts the same way.
  await scenario('paused')
  await waitForText(page, 'Factory reset is in progress')
  assert.equal(await occurrences(page, 'Factory reset is in progress'), 1)
  assert.equal(await page.locator('.issue-card').count(), 0)
  assert.equal(await page.locator('#factory-line, #readout').count(), 0)
  assert.equal(await occurrences(page, 'Waiting for a worker'), 0)

  await scenario('default')
  // A reset suspends the cursor feed; its retry interval is fifteen seconds.
  await page.locator('#factory-line').waitFor({ timeout: 20000 })

  // An unavailable authorization service cannot admit a new page load.
  await scenario('down')
  await page.goto(base + '/')
  await page.waitForURL(base + '/login')
  await waitForText(page, 'Forge couldn’t reach its sign-in service')
  assert.equal(await occurrences(page, 'Waiting for a worker'), 0)
  assert.equal(await page.locator('#factory-line').count(), 0)
  assert.equal(await page.locator('.issue-card').count(), 0)

  // The issues that need the operator are one list, opened from the strip;
  // a deliberate pause (#720) sleeps on the strip's shelf, not in the list.
  // Eleven queued issues are the strip's, not cards.
  await scenario('stacked')
  await page.goto(base + '/')
  await page.locator('#factory-line').waitFor({ timeout: 10000 })
  assert.equal(await page.locator('#needs-attention, #owned, .issue-stack').count(), 0, 'no attention section and no on-hold stack')
  needs = await openNeedsYou(page)
  assert.equal(await needs.locator('.issue-card').count(), 4)
  assert.equal((await needs.locator('[data-needs-title]').innerText()).trim(), '4 need you')
  assert.equal(await needs.locator('.issue-card[data-key="1:700"], .issue-card[data-key="1:720"]').count(), 0, 'neither a queued nor a paused issue is in the list')
  const approval = needs.locator('[data-key="1:730"]')
  assert.match(await approval.locator('canvas').getAttribute('data-look'), /^approval hopeful badge beacon/)
  // A card opened from the list replaces it: the modal closes, the panel
  // takes its place, and the URL carries the issue.
  const listed = needs.locator('.issue-card').first()
  const listedKey = await listed.getAttribute('data-key')
  await listed.click()
  await needs.waitFor({ state: 'hidden', timeout: 10000 })
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  assert.equal(query(page, 'issue'), listedKey)
  await closePanel(page)

  // On a phone-width viewport the cards fill the width and nothing scrolls
  // sideways.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/')
  await page.locator('#factory-line').waitFor({ timeout: 10000 })
  needs = await openNeedsYou(page)
  const width = (await needs.locator('[data-key="1:604"]').boundingBox()).width
  assert.ok(width >= 300, `card is ${width}px wide at 390px viewport`)
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)

  // …and the panel is the whole screen there, still without a sideways scroll.
  panel = await openPanel(page, needs.locator('[data-key="1:604"]'))
  await waitForText(page, DETAIL_TITLE)
  const panelBox = await panel.boundingBox()
  assert.ok(panelBox.width >= 380, `the panel is ${panelBox.width}px wide at 390px viewport`)
  const panelOverflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(
    panelOverflow.scrollWidth <= panelOverflow.innerWidth,
    `the open panel scrolls the page sideways: ${JSON.stringify(panelOverflow)}`,
  )
  await closePanel(page)
}
