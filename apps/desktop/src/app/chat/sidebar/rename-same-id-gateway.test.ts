import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { renameSessionPreferringRpc } from './session-actions-menu'

// This is the UI helper used by the sidebar Rename action. Keep the transport
// entirely mocked: the fixture records which fake gateway would receive the
// mutation and never opens a gateway or calls a provider.
const { activeGateway, renameSession } = vi.hoisted(() => ({
  activeGateway: vi.fn<() => { request: unknown } | null>(() => ({ request: undefined })),
  renameSession: vi.fn(async (_id: string, _title: string, _owner?: unknown) => ({ ok: true, title: 'renamed' }))
}))

vi.mock('@/hermes', () => ({
  HermesGateway: class {},
  renameSession: (...args: [string, string, unknown?]) => renameSession(...args),
  setApiRequestProfile: () => {}
}))

vi.mock('@/store/gateway', () => ({
  $gateway: atom(null),
  activeGateway: () => activeGateway()
}))

const remote = () => ({
  'gateway-a': { title: 'Gateway A' },
  'gateway-b': { title: 'Gateway B' }
})

afterEach(() => {
  renameSession.mockReset()
  renameSession.mockResolvedValue({ ok: true, title: 'renamed' })
  activeGateway.mockReset()
  activeGateway.mockReturnValue({ request: undefined })
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
})

describe('sidebar rename route identity', () => {
  it('renames only the clicked gateway twin and leaves the unrelated remote untouched', async () => {
    const remoteState = remote()
    const clickedRow = { connection_id: 'gateway-b', id: 'sameID', profile: 'default' }

    renameSession.mockImplementation(async (_id, title, owner) => {
      // The real REST transport is intentionally replaced with this local
      // ledger. A profile-only scope models the current ambiguous fallback as
      // the first gateway row; an owner route identifies the clicked twin.
      const connectionId =
        owner && typeof owner === 'object' && 'connectionId' in owner ? String(owner.connectionId) : 'gateway-a'

      remoteState[connectionId as 'gateway-a' | 'gateway-b'].title = title

      return { ok: true, title }
    })

    // A background row takes the REST branch. The clicked row is B, but the
    // current SessionActionsMenu only has id + profile available here.
    $selectedStoredSessionId.set('some-other-session')
    await renameSessionPreferringRpc(clickedRow.id, 'Gateway B renamed', clickedRow.profile)

    expect({
      apiArgs: renameSession.mock.calls[0],
      remote: remoteState
    }).toEqual({
      apiArgs: ['sameID', 'Gateway B renamed', { connectionId: 'gateway-b', profile: 'default' }],
      remote: {
        'gateway-a': { title: 'Gateway A' },
        'gateway-b': { title: 'Gateway B renamed' }
      }
    })
  })
})
