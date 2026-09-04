import { describe, expect, it } from 'vitest'

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
})
