import { useEffect, useMemo, useState } from 'react'

import { browseDirectory, BrowseDirectoryResponse } from '../api'

type DirectoryPickerProps = {
  value: string
  onChange: (value: string) => void
  label?: string
  placeholder?: string
  disabled?: boolean
  mode?: 'directory' | 'file'
}

function getInitialPath(value: string) {
  const trimmed = value.trim()
  return trimmed || undefined
}

function joinPath(base: string, name: string): string {
  const sep = base.includes('\\') ? '\\' : '/'
  return base.endsWith(sep) ? `${base}${name}` : `${base}${sep}${name}`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export function DirectoryPicker({
  value,
  onChange,
  label,
  placeholder,
  disabled = false,
  mode = 'directory',
}: DirectoryPickerProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [browser, setBrowser] = useState<BrowseDirectoryResponse | null>(null)
  const [selectedFile, setSelectedFile] = useState<string>('')
  const initialPath = useMemo(() => getInitialPath(value), [value])

  const isFileMode = mode === 'file'

  const load = async (path?: string) => {
    setLoading(true)
    try {
      const result = await browseDirectory(path, isFileMode ? 'both' : 'directory')
      setBrowser(result)
      if (isFileMode && result.current) {
        setSelectedFile(result.current)
      }
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '目录读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!open) return
    void load(initialPath)
  }, [initialPath, open])

  useEffect(() => {
    if (!open) setSelectedFile('')
  }, [open])

  const handleSelect = () => {
    if (isFileMode) {
      if (selectedFile) {
        onChange(selectedFile)
        setOpen(false)
      }
      return
    }
    if (browser?.current) {
      onChange(browser.current)
      setOpen(false)
    }
  }

  return (
    <div className="directory-picker">
      {label ? <span>{label}</span> : null}
      <div className="directory-picker-row">
        <input value={value} placeholder={placeholder} disabled={disabled} onChange={(event) => onChange(event.target.value)} />
        <button type="button" className="ghost-button" disabled={disabled} onClick={() => setOpen(true)}>
          浏览
        </button>
      </div>
      {open ? (
        <div className="dialog-backdrop" role="presentation" onClick={() => setOpen(false)}>
          <div className="dialog-card" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <div className="card-header">
              <div>
                <h3>{isFileMode ? '选择视频文件' : '选择目录'}</h3>
                <p className="muted">{browser?.current || '加载中…'}</p>
              </div>
              <button type="button" className="text-button" onClick={() => setOpen(false)}>
                关闭
              </button>
            </div>
            {error ? <div className="alert error">{error}</div> : null}
            <div className="directory-browser-toolbar">
              <button type="button" className="ghost-button" disabled={loading || !browser?.parent} onClick={() => void load(browser?.parent || undefined)}>
                上一级
              </button>
              {isFileMode ? (
                <button type="button" disabled={!selectedFile} onClick={handleSelect}>
                  选择当前路径
                </button>
              ) : (
                <button type="button" disabled={loading || !browser?.current} onClick={handleSelect}>
                  选择此目录
                </button>
              )}
            </div>
            <div className="directory-browser-list">
              {loading ? <div className="muted">目录加载中…</div> : null}
              {!loading && (browser?.dirs.length ?? 0) === 0 && (browser?.files?.length ?? 0) === 0 ? (
                <div className="muted">当前目录为空</div>
              ) : null}
              {!loading
                ? browser?.dirs.map((name) => {
                    const nextPath = browser.current ? joinPath(browser.current, name) : name
                    return (
                      <button key={nextPath} type="button" className="directory-entry" onClick={() => void load(nextPath)}>
                        📁 {name}
                      </button>
                    )
                  })
                : null}
              {isFileMode && !loading
                ? browser?.files?.map((file) => {
                    const fullPath = browser.current ? joinPath(browser.current, file.name) : file.name
                    const isSelected = selectedFile === fullPath
                    return (
                      <button
                        key={fullPath}
                        type="button"
                        className={isSelected ? 'directory-entry directory-entry-selected' : 'directory-entry'}
                        onClick={() => setSelectedFile(fullPath)}
                      >
                        <span className="file-icon">🎬</span>
                        <span className="file-name">{file.name}</span>
                        <span className="file-size muted">{formatBytes(file.size_bytes)}</span>
                      </button>
                    )
                  })
                : null}
            </div>
            <div className="page-actions">
              <button type="button" className="ghost-button" onClick={() => setOpen(false)}>
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
