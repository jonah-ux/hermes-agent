import assert from 'node:assert/strict'

import { test } from 'vitest'

import { fetchRegistrySessionRows, fetchRemoteProfileSessions } from './profile-session-routing'

test('remote registry fanout preserves valid rows when a sibling gateway times out', async () => {
  const calls: Array<{ descriptor: unknown; path: string }> = []

  const rows = await fetchRegistrySessionRows(
    [
      { connectionId: 'gw-timeout', kind: 'ssh', backends: [{ descriptor: 'timeout', profileLabel: 'slow' }] },
      { connectionId: 'gw-live', kind: 'ssh', backends: [{ descriptor: 'live', profileLabel: 'fast' }] }
    ],
    new URLSearchParams({ profile: 'all', limit: '20', offset: '0' }),
    async (descriptor, path) => {
      calls.push({ descriptor, path })

      if (descriptor === 'timeout') {
        await new Promise(resolve => setTimeout(resolve, 5))
        throw new Error('ETIMEDOUT')
      }

      return { sessions: [{ id: 'live-session' }], total: 1 }
    }
  )

  assert.deepEqual(
    rows.map(row => ({
      id: (row as { id: string }).id,
      connection_id: (row as { connection_id: string }).connection_id,
      profile: (row as { profile: string }).profile
    })),
    [{ id: 'live-session', connection_id: 'gw-live', profile: 'fast' }]
  )
  assert.deepEqual(
    calls.map(call => call.descriptor),
    ['timeout', 'live']
  )
  assert.ok(calls.every(call => call.path === '/api/sessions?limit=20&offset=0'))
})

test('remote profile timeout rejects instead of becoming an empty successful response', async () => {
  const timeout = new Error('ETIMEDOUT')

  await assert.rejects(
    fetchRemoteProfileSessions(
      'remote-timeout',
      new URLSearchParams({ profile: 'remote-timeout', limit: '20', offset: '0' }),
      async () => {
        await new Promise(resolve => setTimeout(resolve, 5))
        throw timeout
      }
    ),
    error => error === timeout
  )
})
