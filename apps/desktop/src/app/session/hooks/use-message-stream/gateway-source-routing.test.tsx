import { type GatewayEvent, JsonRpcGatewayClient } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { chatMessageText } from '@/lib/chat-messages'
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

function event(text: string): GatewayEvent {
  return { payload: { text }, session_id: SESSION_ID, type: 'message.delta' }
}

function tagged(connectionId: string, sourceEvent: GatewayEvent): RpcEvent {
  return { ...sourceEvent, connectionId, profile: 'default' }
}

function assistantText(stream: MessageStreamHarness): string {
  return stream
    .state()
    .messages.filter(message => message.role === 'assistant' && !message.hidden)
    .map(chatMessageText)
    .join('')
}

afterEach(() => {
  cleanup()
  FakeSocket.instances = []
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

  it.fails('isolates same-session chats through the current Electron global fan-in', async () => {
    vi.useFakeTimers()
    const statesA = new Map<string, ReturnType<MessageStreamHarness['state']>>()
    const statesB = new Map<string, ReturnType<MessageStreamHarness['state']>>()
    const streamA = renderMessageStream(SESSION_ID, { states: statesA })
    const streamB = renderMessageStream(SESSION_ID, { states: statesB })
    const gatewayA = await connectGateway('gateway-a')
    const gatewayB = await connectGateway('gateway-b')

    // This is the current use-gateway-boot/createSecondary shape: each socket
    // tags its frame, then both callbacks feed one global handleGatewayEvent.
    const globalFanIn = (frame: RpcEvent) => streamA.handleEvent(frame)
    const offA = gatewayA.client.onEvent(frame => globalFanIn(tagged('gateway-a', frame)))
    const offB = gatewayB.client.onEvent(frame => globalFanIn(tagged('gateway-b', frame)))

    act(() => {
      gatewayA.socket.serverEvent(event('A1'))
      gatewayB.socket.serverEvent(event('B1'))
      gatewayA.socket.serverEvent(event('A2'))
      gatewayB.socket.serverEvent(event('B2'))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STREAM_DELTA_FLUSH_MS)
    })

    // The desired proof is one transcript per (connection, profile, session).
    // On 2ddd both source-tagged streams resolve to the bare session id, so
    // the current fan-in appends gateway B into gateway A and leaves B empty.
    expect(assistantText(streamA)).toBe('A1A2')
    expect(assistantText(streamB)).toBe('B1B2')

    offA()
    offB()
    gatewayA.client.close()
    gatewayB.client.close()
  })
})
