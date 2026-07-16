import { render, screen, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DirectoryPicker } from '../components/DirectoryPicker'

vi.mock('../api', () => ({
  browseDirectory: vi.fn().mockResolvedValue({
    current: '/data',
    parent: undefined,
    dirs: ['movies', 'shows'],
  }),
}))

function renderPicker(props?: Partial<React.ComponentProps<typeof DirectoryPicker>>) {
  return render(
    <DirectoryPicker
      value="/data"
      onChange={() => {}}
      label="输入目录"
      {...props}
    />,
  )
}

describe('DirectoryPicker P1-10: a11y dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not render dialog when closed', () => {
    const { container } = renderPicker()
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('renders dialog with role and aria-modal when open', async () => {
    renderPicker()
    const browseBtn = screen.getByText('浏览')
    fireEvent.click(browseBtn)

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })

  it('closes dialog on Escape key', async () => {
    renderPicker()
    fireEvent.click(screen.getByText('浏览'))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('has aria-labelledby referencing the dialog title', async () => {
    renderPicker()
    fireEvent.click(screen.getByText('浏览'))
    const dialog = await screen.findByRole('dialog')

    const labelledBy = dialog.getAttribute('aria-labelledby')
    expect(labelledBy).toBeTruthy()

    const labelledEl = document.getElementById(labelledBy!)
    expect(labelledEl).not.toBeNull()
    expect(labelledEl?.textContent).toContain('选择目录')
  })

  it('does not close on non-Escape key', async () => {
    renderPicker()
    fireEvent.click(screen.getByText('浏览'))
    const dialog = await screen.findByRole('dialog')

    fireEvent.keyDown(dialog, { key: 'Enter' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('focuses first focusable element on open', async () => {
    renderPicker()
    fireEvent.click(screen.getByText('浏览'))
    await screen.findByRole('dialog')

    const focused = document.activeElement
    expect(focused).not.toBe(document.body)
  })
})
