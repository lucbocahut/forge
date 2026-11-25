import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/settings')
  const card = page.locator('#updates')
  await card.getByText('An update is available.', { exact: true }).waitFor()
  assert.match(await card.innerText(), /Installed: a{12}.*Latest: b{12}/)
  assert.match(await card.getByRole('link', { name: 'Release notes' }).getAttribute('href'), /github.com\/lucbocahut\/forge\/releases\/tag/)
  await card.getByRole('button', { name: 'Check for updates' }).click()
  await page.waitForFunction(() => !document.querySelector('#updates button')?.disabled)
  const reads = (await requests()).filter(row => row.path === '/api/updates')
  assert.equal(reads.length, 2)
  assert.equal(reads[1].query.refresh, '1')
  assert.ok(reads.every(row => row.method === 'GET'))
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 })
    await card.scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: `/tmp/forge-updates-${width}.png`, fullPage: true, animations: 'disabled' })
  }

  await scenario('updates-private')
  await page.goto(base + '/settings')
  await card.getByText('GitHub releases are not publicly accessible yet.', { exact: true }).waitFor()
  assert.equal(await card.getByRole('link', { name: 'Update instructions' }).count(), 0)
}
