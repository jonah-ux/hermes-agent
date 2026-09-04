import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { formatErrorDiagnostics } from '@/lib/error-surface'
import { $notifications, clearNotifications, readableError } from '@/store/notifications'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'synthetic-redaction-session'
const SYNTHETIC_SECRET = 'synthetic-hermes-session-credential-9f2c4a'
const ERROR_SURFACE = { layer: 'gateway' as const, code: 'gateway_transport_failed', retryable: true }

const RAW_ERROR =
  `HTTP 503 gateway_transport_failed type=transport_error ` +
  `url=https://gateway.example.test/api/ws?ticket=${SYNTHETIC_SECRET}&attempt=1 ` +
  `config API_KEY=${SYNTHETIC_SECRET}`

let stream: MessageStreamHarness

function mountStream() {
  stream = renderMessageStream(SID)
}

function lastAssistant(): ClientSessionState['messages'][number] | undefined {
  return [...stream.state().messages].reverse().find(message => message.role === 'assistant' && !message.hidden)
}

afterEach(() => {
  cleanup()
  clearNotifications()
})

describe('Hermes failed transport/config redaction contract', () => {
  it('redacts terminal failure text while retaining the typed error surface', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          error: RAW_ERROR,
          error_surface: ERROR_SURFACE,
          status: 'error',
          text: 'Error: gateway transport failed'
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    const bubble = lastAssistant()

    expect(bubble?.error).not.toContain(SYNTHETIC_SECRET)
    expect(bubble?.error).toContain('gateway_transport_failed')
    expect(bubble?.error).toContain('HTTP 503')
    expect(bubble?.errorSurface).toEqual(ERROR_SURFACE)
  })

  it('redacts explicit gateway errors from the transcript and toast surfaces', () => {
    mountStream()

    act(() => stream.handleEvent({ payload: { message: RAW_ERROR }, session_id: SID, type: 'error' }))

    const bubble = lastAssistant()
    const notifications = $notifications.get()

    expect(bubble?.error).not.toContain(SYNTHETIC_SECRET)
    expect(bubble?.error).toContain('gateway_transport_failed')
    expect(notifications).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ message: expect.stringContaining(SYNTHETIC_SECRET) })
      ])
    )
    expect(notifications).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ detail: expect.stringContaining(SYNTHETIC_SECRET) })
      ])
    )
  })

  it('redacts copy/send diagnostics without dropping typed recovery context', () => {
    const diagnostics = formatErrorDiagnostics({ errorText: RAW_ERROR, surface: ERROR_SURFACE })

    expect(diagnostics).not.toContain(SYNTHETIC_SECRET)
    expect(diagnostics).toContain('layer: gateway')
    expect(diagnostics).toContain('code: gateway_transport_failed')
    expect(diagnostics).toContain('retryable: true')
    expect(diagnostics).toContain('HTTP 503')
  })

  it('keeps readable error details useful while removing synthetic credentials', () => {
    const readable = readableError(new Error(RAW_ERROR), 'Gateway connection failed')
    const surfaced = [readable.message, readable.detail].filter(Boolean).join('\n')

    expect(surfaced).not.toContain(SYNTHETIC_SECRET)
    expect(surfaced).toContain('gateway_transport_failed')
    expect(surfaced).toContain('HTTP 503')
  })
})
