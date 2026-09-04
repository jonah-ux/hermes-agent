import { type GatewayEvent, JsonRpcGatewayClient } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { chatMessageText } from '@/lib/chat-messages'
import {
  closeSecondaryGateways,
  configureGatewayRegistry,
  disposeSecondariesForConnection,
  retainGatewayForAgent
} from '@/store/gateway'
import type { RpcEvent } from '@/types/hermes'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'
import { STREAM_DELTA_FLUSH_MS } from './utils'

const SESSION_ID = 'same-session-on-two-gateways'

type SocketListener = (event: { data?: string }) => void

class FakeSocket {
  static instances: FakeSocket[] = []

  readyState = 0
  private readonly listeners = new Map<string, Set<SocketListener>>()

  constructor(readonly url: string) {
    FakeSocket.instances.push(this)
  }

  addEventListener(type: string, listener: SocketListener): void {
    const listeners = this.listeners.get(type) ?? new Set<SocketListener>()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  removeEventListener(type: string, listener: SocketListener): void {
    this.listeners.get(type)?.delete(listener)
  }

  send(_payload: string): void {
    // The proof only drives server-to-client event frames.
  }

  close(): void {
    this.readyState = 3
    this.emit('close', {})
  }

  open(): void {
    this.readyState = 1
    this.emit('open', {})
  }

  serverEvent(event: GatewayEvent): void {
    this.emit('message', { data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params: event }) })
  }

  private emit(type: string, event: { data?: string }): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) {
      listener(event)
    }
  }
}

interface ConnectedGateway {
  client: JsonRpcGatewayClient
  socket: FakeSocket
}

async function connectGateway(name: string): Promise<ConnectedGateway> {
  const client = new JsonRpcGatewayClient({
    connectTimeoutMs: 1_000,
    heartbeatDeadlineMs: 0,
    heartbeatIntervalMs: 0,
    socketFactory: url => new FakeSocket(url) as unknown as WebSocket
  })

  const connected = client.connect(`ws://${name}.invalid/api/ws`)
  const socket = FakeSocket.instances.at(-1)

  if (!socket) {
    throw new Error(`no socket was created for ${name}`)
  }

  socket.open()
  await connected

  return { client, socket }
}

interface RetainedGateway {
  release: () => void
  socket: FakeSocket
}

async function retainRoutedGateway(connectionId: string): Promise<RetainedGateway> {
  const socketIndex = FakeSocket.instances.length
  const opening = retainGatewayForAgent(connectionId, 'default')
  let socket: FakeSocket | undefined

  for (let attempt = 0; attempt < 20 && !socket; attempt += 1) {
    socket = FakeSocket.instances[socketIndex]

    if (!socket) {
      await Promise.resolve()
    }
  }

  if (!socket) {
    throw new Error(`no routed socket was created for ${connectionId}`)
  }

  socket.open()

  return { release: await opening, socket }
}

function installRegistryDesktop(): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    getConnectionFor: vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) => ({
      authMode: 'token',
      connectionId,
      profile,
      token: 'test-token',
      wsUrl: `wss://${connectionId}.invalid/api/ws?profile=${profile}`
    })),
    getGatewayWsUrlFor: vi.fn(
      async ({ connectionId, profile }: { connectionId: string; profile: string }) =>
        `wss://${connectionId}.invalid/api/ws?profile=${profile}`
    ),
    touchBackend: vi.fn(async () => undefined)
  }
}

function event(text: string): GatewayEvent {
  return { payload: { text }, session_id: SESSION_ID, type: 'message.delta' }
}

function tagged(connectionId: string, sourceEvent: GatewayEvent): RpcEvent {
  return { ...sourceEvent, connectionId, profile: 'default' }
}

function assistantText(stream: MessageStreamHarness): string {
  return assistantTextFromState(stream.state())
}

function assistantTextFromState(state: ReturnType<MessageStreamHarness['state']>): string {
  return state.messages
    .filter(message => message.role === 'assistant' && !message.hidden)
    .map(chatMessageText)
    .join('')
}

afterEach(() => {
  cleanup()
  closeSecondaryGateways()
  FakeSocket.instances = []
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
  vi.useRealTimers()
})

describe('Electron gateway stream source routing', () => {
  it('keeps interleaved same-session frames on their owning gateway subscription', async () => {
    const gatewayA = await connectGateway('gateway-a')
    const gatewayB = await connectGateway('gateway-b')
    const receivedA: string[] = []
    const receivedB: string[] = []
    const offA = gatewayA.client.onEvent(frame => receivedA.push(String((frame.payload as { text?: string })?.text)))
    const offB = gatewayB.client.onEvent(frame => receivedB.push(String((frame.payload as { text?: string })?.text)))

    gatewayA.socket.serverEvent(event('A1'))
    gatewayB.socket.serverEvent(event('B1'))
    gatewayA.socket.serverEvent(event('A2'))
    gatewayB.socket.serverEvent(event('B2'))

    expect(receivedA).toEqual(['A1', 'A2'])
    expect(receivedB).toEqual(['B1', 'B2'])

    offA()
    gatewayA.socket.serverEvent(event('A-after-unsubscribe'))
    gatewayB.socket.serverEvent(event('B-after-A-unsubscribe'))

    expect(receivedA).toEqual(['A1', 'A2'])
    expect(receivedB).toEqual(['B1', 'B2', 'B-after-A-unsubscribe'])

    gatewayA.client.close()
    gatewayB.client.close()
  })

  it('appends interleaved frames to separate chats when each source owns its sink', async () => {
    vi.useFakeTimers()
    const statesA = new Map<string, ReturnType<MessageStreamHarness['state']>>()
    const statesB = new Map<string, ReturnType<MessageStreamHarness['state']>>()
    const streamA = renderMessageStream(SESSION_ID, { states: statesA })
    const streamB = renderMessageStream(SESSION_ID, { states: statesB })
    const gatewayA = await connectGateway('gateway-a')
    const gatewayB = await connectGateway('gateway-b')
    const offA = gatewayA.client.onEvent(frame => streamA.handleEvent(tagged('gateway-a', frame)))
    const offB = gatewayB.client.onEvent(frame => streamB.handleEvent(tagged('gateway-b', frame)))

    act(() => {
      gatewayA.socket.serverEvent(event('A1'))
      gatewayB.socket.serverEvent(event('B1'))
      gatewayA.socket.serverEvent(event('A2'))
      gatewayB.socket.serverEvent(event('B2'))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STREAM_DELTA_FLUSH_MS)
    })

    expect(assistantText(streamA)).toBe('A1A2')
    expect(assistantText(streamB)).toBe('B1B2')

    offA()
    act(() => gatewayA.socket.serverEvent(event('A-late')))
    expect(assistantText(streamA)).toBe('A1A2')

    gatewayA.client.close()
    gatewayB.client.close()
    offB()
  })

  it.fails('isolates same-session chats through the current Electron registry fan-in', async () => {
    vi.useFakeTimers()
    installRegistryDesktop()
    const states = new Map<string, ReturnType<MessageStreamHarness['state']>>()
    const stream = renderMessageStream(SESSION_ID, { states })
    const observedSources: string[] = []

    // Exercise the production registry boundary: retainGatewayForAgent creates
    // the private createSecondary entries, whose real JsonRpcGatewayClient
    // listeners call configureGatewayRegistry.onEvent. This is the same global
    // callback installed by use-gateway-boot, not a test-created per-socket
    // callback.
    configureGatewayRegistry({
      onEvent: event => {
        observedSources.push(
          `${event.connectionId ?? 'unscoped'}:${String((event.payload as { text?: string })?.text)}`
        )
        stream.handleEvent(event as RpcEvent)
      }
    })

    const gatewayA = await retainRoutedGateway('gateway-a')
    const gatewayB = await retainRoutedGateway('gateway-b')

    act(() => {
      gatewayA.socket.serverEvent(event('A1'))
      gatewayB.socket.serverEvent(event('B1'))
      gatewayA.socket.serverEvent(event('A2'))
      gatewayB.socket.serverEvent(event('B2'))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STREAM_DELTA_FLUSH_MS)
    })

    expect(observedSources).toEqual(['gateway-a:A1', 'gateway-b:B1', 'gateway-a:A2', 'gateway-b:B2'])
    expect(assistantText(stream)).toBe('A1B1A2B2')

    // The production connection teardown must remove only gateway A's real
    // secondary listener; gateway B must remain subscribed.
    disposeSecondariesForConnection('gateway-a')
    act(() => {
      gatewayA.socket.serverEvent(event('A-after-unsubscribe'))
      gatewayB.socket.serverEvent(event('B-after-A-unsubscribe'))
    })

    expect(observedSources).toEqual([
      'gateway-a:A1',
      'gateway-b:B1',
      'gateway-a:A2',
      'gateway-b:B2',
      'gateway-b:B-after-A-unsubscribe'
    ])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STREAM_DELTA_FLUSH_MS)
    })

    // Desired contract: the same session id is isolated by source as well as
    // session. On immutable 2ddd the real global consumer only has the bare
    // session key, so these route-keyed assertions remain an expected failure.
    expect(assistantTextFromState(stream.state('conn:gateway-a::default\u0000same-session-on-two-gateways'))).toBe(
      'A1A2'
    )
    expect(assistantTextFromState(stream.state('conn:gateway-b::default\u0000same-session-on-two-gateways'))).toBe(
      'B1B2B-after-A-unsubscribe'
    )

    gatewayA.release()
    gatewayB.release()
  })
})
