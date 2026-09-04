import { describe, expect, it } from 'vitest'

import { makeNousCloudBackendDownError, makeReauthRequiredError } from './backend-health'
import { gatewayWsUrlIpcResult } from './connection-config'
import { formatRendererBoundaryReport, formatRendererConsoleLine } from './renderer-log'
import { redactSecrets } from './ssh-connection'

const SYNTHETIC_SECRET = 'synthetic-hermes-transport-credential-7a1b3c'

const RAW_TRANSPORT_ERROR =
  `gateway_transport_failed type=transport_error url=https://gateway.example.test/api/ws?token=${SYNTHETIC_SECRET}`

const RAW_CONFIG_ERROR = `gateway_transport_failed config API_KEY=${SYNTHETIC_SECRET} HTTP 503`

describe('Hermes failed transport/config persistence redaction contract', () => {
  it('redacts URL credentials before renderer console lines reach desktop.log', () => {
    const line = formatRendererConsoleLine('session', {
      level: 3,
      lineNumber: 42,
      message: RAW_TRANSPORT_ERROR,
      sourceUrl: 'file:///hermes/renderer.js'
    })

    expect(line).not.toContain(SYNTHETIC_SECRET)
    expect(line).toContain('gateway_transport_failed')
    expect(line).toContain('type=transport_error')
  })

  it('redacts config values in renderer crash reports while retaining status/type', () => {
    const report = formatRendererBoundaryReport('session', 'root', RAW_CONFIG_ERROR, 'at Gateway (gateway.js:1)')

    expect(report).not.toContain(SYNTHETIC_SECRET)
    expect(report).toContain('gateway_transport_failed')
    expect(report).toContain('HTTP 503')
    expect(report).toContain('at Gateway (gateway.js:1)')
  })

  it('requires the shared SSH/log redaction seam to preserve typed context', () => {
    const redacted = redactSecrets(`${RAW_TRANSPORT_ERROR} ${RAW_CONFIG_ERROR}`)

    expect(redacted).not.toContain(SYNTHETIC_SECRET)
    expect(redacted).toContain('gateway_transport_failed')
    expect(redacted).toContain('HTTP 503')
  })

  it('redacts the detail carried by a terminal reauth wrapper', () => {
    const error = makeReauthRequiredError(RAW_TRANSPORT_ERROR) as Error & {
      detail?: string
      needsOauthLogin?: boolean
    }

    expect(error.message).toMatch(/remote gateway session has expired/i)
    expect(error.detail).not.toContain(SYNTHETIC_SECRET)
    expect(error.detail).toContain('gateway_transport_failed')
    expect(error.needsOauthLogin).toBe(true)
  })

  it('redacts the detail carried by a typed Cloud-down transport error', () => {
    const source = new Error(RAW_TRANSPORT_ERROR) as Error & { statusCode?: number }
    source.statusCode = 503

    const error = makeNousCloudBackendDownError('https://synthetic.agents.nousresearch.com', source) as Error & {
      cause?: unknown
      detail?: string
      isCloudBackendDown?: boolean
      statusCode?: number
    }

    expect(error).not.toBeNull()
    expect(error.message).not.toContain(SYNTHETIC_SECRET)
    expect(error.detail).not.toContain(SYNTHETIC_SECRET)
    expect(error.message).toContain('HTTP 503')
    expect(error.detail).toContain('gateway_transport_failed')
    expect(error.isCloudBackendDown).toBe(true)
    expect(error.statusCode).toBe(503)
    expect(error.cause).toBe(source)
  })

  it('redacts a failed gateway WS-url IPC result without dropping its type', async () => {
    const result = await gatewayWsUrlIpcResult(async () => {
      throw new Error(RAW_TRANSPORT_ERROR)
    })

    if (result.ok) {
      throw new Error('expected gateway WS-url failure')
    }

    expect(result.error).not.toContain(SYNTHETIC_SECRET)
    expect(result.error).toContain('gateway_transport_failed')
    expect(result.error).toContain('type=transport_error')
  })
})
