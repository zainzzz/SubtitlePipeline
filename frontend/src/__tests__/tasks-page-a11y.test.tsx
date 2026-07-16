import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api'
import { TasksPage } from '../pages/TasksPage'

vi.mock('../api', () => ({
  getTasks: vi.fn(),
  getScanStatus: vi.fn(),
  checkResumeFeasibility: vi.fn(),
  cancelTask: vi.fn().mockResolvedValue(undefined),
  retryTask: vi.fn().mockResolvedValue(undefined),
  deleteTask: vi.fn().mockResolvedValue(undefined),
  setScanEnabled: vi.fn(),
}))

const tasksResponse = {
  items: [
    {
      id: 42,
      file_path: '/data/movie.mp4',
      status: 'failed',
      stage: 'run_asr',
      progress: 50,
      retry_count: 1,
      max_retries: 3,
      cancel_requested: 0,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    },
    {
      id: 7,
      file_path: '/data/processing.mp4',
      status: 'processing',
      stage: 'run_asr',
      progress: 30,
      retry_count: 0,
      max_retries: 3,
      cancel_requested: 0,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    },
  ],
  total: 2,
  page: 1,
  page_size: 20,
  status_counts: { failed: 1, processing: 1 },
}

const scanStatusResponse = {
  last_scan_at: null,
  scanned: 0,
  queued: 0,
  skipped: 0,
  pending_count: 0,
  throttled: false,
  scan_enabled: true,
}

function setupMocks() {
  vi.mocked(api.getTasks).mockResolvedValue(tasksResponse)
  vi.mocked(api.getScanStatus).mockResolvedValue(scanStatusResponse)
  vi.mocked(api.checkResumeFeasibility).mockResolvedValue({ can_resume: true, missing: [] })
}

function renderTasksPage() {
  return render(
    <MemoryRouter>
      <TasksPage />
    </MemoryRouter>,
  )
}

describe('TasksPage P1-9: accessibility', () => {
  beforeEach(() => {
    setupMocks()
  })

  it('renders table with aria-label', async () => {
    const { container } = renderTasksPage()
    await waitFor(() => expect(api.getTasks).toHaveBeenCalled())
    const table = container.querySelector('table')
    expect(table).toBeInTheDocument()
    expect(table?.getAttribute('aria-label')).toBe('任务列表')
  })

  it('all th elements have scope="col"', async () => {
    const { container } = renderTasksPage()
    await waitFor(() => expect(api.getTasks).toHaveBeenCalled())
    const headers = container.querySelectorAll('thead th')
    expect(headers.length).toBeGreaterThanOrEqual(7)
    headers.forEach((th) => {
      expect(th.getAttribute('scope')).toBe('col')
    })
  })

  it('restart button has descriptive aria-label with task id', async () => {
    renderTasksPage()
    const btn = await screen.findByLabelText('重新执行 任务 42')
    expect(btn).toBeInTheDocument()
    expect(btn.tagName).toBe('BUTTON')
  })

  it('delete button has descriptive aria-label with task id', async () => {
    renderTasksPage()
    const btn = await screen.findByLabelText('删除 任务 42')
    expect(btn).toBeInTheDocument()
  })

  it('cancel button on processing task has aria-label', async () => {
    renderTasksPage()
    const btn = await screen.findByLabelText('取消 任务 7')
    expect(btn).toBeInTheDocument()
  })

  it('resume button has aria-label with task id', async () => {
    renderTasksPage()
    const btn = await screen.findByLabelText('继续执行 任务 42')
    expect(btn).toBeInTheDocument()
  })
})
