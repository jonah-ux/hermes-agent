import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { deleteSession, type SessionInfo, setSessionArchived } from '@/hermes'
import { $sessions, setSessions } from '@/store/session'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from '.'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn().mockResolvedValue(undefined),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

type HarnessHandle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession' | 'removeSession'>

type RemoteState = {
  archived: boolean
  deleted: boolean
}

type GatewayId = 'gateway-a' | 'gateway-b'

function session(connection_id: GatewayId, title: string): SessionInfo {
  return {
    connection_id,
    ended_at: null,
    id: 'sameID',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: 'desktop',
    started_at: 1,
    title,
    tool_call_count: 0
  } as SessionInfo
}

function remoteState(): Record<GatewayId, RemoteState> {
  return {
    'gateway-a': { archived: false, deleted: false },
    'gateway-b': { archived: false, deleted: false }
  }
}

function mockDeleteMutation(remote: Record<GatewayId, RemoteState>) {
  vi.mocked(deleteSession).mockImplementation(async (_id, owner) => {
    // The real API is intentionally not reached. A route-aware mock records
    // which gateway the action would mutate; a bare/profile-only scope models
    // the current ambiguous fallback as the first gateway row.
    const connectionId =
      owner && typeof owner === 'object' && 'connectionId' in owner ? String(owner.connectionId) : 'gateway-a'

    remote[connectionId as GatewayId].deleted = true

    return { ok: true }
  })
}

function mockArchiveMutation(remote: Record<GatewayId, RemoteState>) {
  vi.mocked(setSessionArchived).mockImplementation(async (_id, _archived, owner) => {
    const scopedOwner = owner as unknown

    const connectionId =
      scopedOwner && typeof scopedOwner === 'object' && 'connectionId' in scopedOwner
        ? String((scopedOwner as { connectionId: unknown }).connectionId)
        : 'gateway-a'

    remote[connectionId as GatewayId].archived = true

    return { ok: true }
  })
}

function Harness({ onReady }: { onReady: (actions: HarnessHandle) => void }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway: vi.fn(async () => ({}) as never),
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions)
  }, [actions, onReady])

  return null
}

async function renderActions(): Promise<HarnessHandle> {
  let actions: HarnessHandle | null = null

  render(<Harness onReady={value => (actions = value)} />)
  await waitFor(() => expect(actions).not.toBeNull())

  return actions!
}

afterEach(() => {
  cleanup()
  setSessions([])
  vi.clearAllMocks()
})

describe('sidebar same-id gateway mutation routing', () => {
  it('deletes only the clicked gateway twin and keeps the unrelated remote row', async () => {
    const remote = remoteState()
    mockDeleteMutation(remote)

    const clickedRow = session('gateway-b', 'Gateway B')
    setSessions([session('gateway-a', 'Gateway A'), clickedRow])
    const actions = await renderActions()

    // This is the bare-id argument currently wired by SidebarSessionsSection
    // for clickedRow. The hook must preserve that row's exact owner.
    await act(async () => {
      await actions.removeSession(clickedRow.id)
    })

    expect({
      apiOwner: vi.mocked(deleteSession).mock.calls[0]?.[1],
      remainingRows: $sessions.get().map(row => row.connection_id),
      remote
    }).toEqual({
      apiOwner: { connectionId: 'gateway-b', profile: 'default' },
      remainingRows: ['gateway-a'],
      remote: {
        'gateway-a': { archived: false, deleted: false },
        'gateway-b': { archived: false, deleted: true }
      }
    })
  })

  it('archives only the clicked gateway twin and keeps the unrelated remote row', async () => {
    const remote = remoteState()
    mockArchiveMutation(remote)

    const clickedRow = session('gateway-b', 'Gateway B')
    setSessions([session('gateway-a', 'Gateway A'), clickedRow])
    const actions = await renderActions()

    // Same hostile fixture, but through the Archive menu action. No gateway,
    // network, or provider is touched: setSessionArchived is the mock seam.
    await act(async () => {
      await actions.archiveSession(clickedRow.id)
    })

    expect({
      apiOwner: vi.mocked(setSessionArchived).mock.calls[0]?.[2],
      remainingRows: $sessions.get().map(row => row.connection_id),
      remote
    }).toEqual({
      apiOwner: { connectionId: 'gateway-b', profile: 'default' },
      remainingRows: ['gateway-a'],
      remote: {
        'gateway-a': { archived: false, deleted: false },
        'gateway-b': { archived: true, deleted: false }
      }
    })
  })
})
