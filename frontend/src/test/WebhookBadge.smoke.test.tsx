/**
 * Smoke test: render a real React component to verify the full chain
 * (vitest + jsdom + @testing-library/react + @testing-library/jest-dom) is
 * wired up. Also serves as a template for future component tests.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

// A tiny inline component to test the harness without taking a dependency
// on a specific page (which might not be importable in isolation due to
// router / context coupling). When real component tests land, replace this
// with `import { WebhookBadge } from '../pages/TaskDetailPage'` etc.
function Greeting({ name }: { name: string }) {
  return <h1>hello, {name}</h1>
}

describe('react testing harness', () => {
  it('renders a component and exposes its text via testing-library', () => {
    render(<Greeting name="world" />)
    // toBeInTheDocument comes from @testing-library/jest-dom/vitest
    // (loaded via src/test/setup.ts).
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument()
    expect(screen.getByText(/hello, world/)).toBeInTheDocument()
  })

  it('re-renders when props change', () => {
    const { rerender } = render(<Greeting name="alice" />)
    expect(screen.getByText(/alice/)).toBeInTheDocument()
    rerender(<Greeting name="bob" />)
    expect(screen.getByText(/bob/)).toBeInTheDocument()
  })
})
