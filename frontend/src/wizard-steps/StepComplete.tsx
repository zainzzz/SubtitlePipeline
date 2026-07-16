interface StepCompleteProps {
  outputModeLabel: string
  selectedModel: string
  sourceLanguageLabel: string
  bilingualModeLabel: string
  translationContentTypeLabel: string
  finishing: boolean
  onComplete: () => void
  onPrev: () => void
  bilingualEnabled: boolean
  translationEnabled: boolean
  translationModel: string
  targetLanguages: string
}

export function StepComplete({
  outputModeLabel,
  selectedModel,
  sourceLanguageLabel,
  bilingualModeLabel,
  translationContentTypeLabel,
  finishing,
  onComplete,
  onPrev,
  bilingualEnabled,
  translationEnabled,
  translationModel,
  targetLanguages,
}: StepCompleteProps) {
  return (
    <div className="card">
      <h2>步骤 5：完成确认</h2>
      <div className="summary-grid">
        <div className="summary-item">
          <span>输出位置</span>
          <strong>{outputModeLabel}</strong>
        </div>
        <div className="summary-item">
          <span>模型</span>
          <strong>{selectedModel || '-'}</strong>
        </div>
        <div className="summary-item">
          <span>源语言</span>
          <strong>{sourceLanguageLabel}</strong>
        </div>
        <div className="summary-item">
          <span>双语字幕</span>
          <strong>{bilingualEnabled ? '已开启' : '已关闭'}</strong>
        </div>
        <div className="summary-item">
          <span>双语模式</span>
          <strong>{bilingualEnabled ? bilingualModeLabel : '-'}</strong>
        </div>
        <div className="summary-item">
          <span>翻译</span>
          <strong>{translationEnabled ? '已启用' : '已关闭'}</strong>
        </div>
        <div className="summary-item">
          <span>翻译模型</span>
          <strong>{translationEnabled ? translationModel : '-'}</strong>
        </div>
        <div className="summary-item">
          <span>目标语言</span>
          <strong>{translationEnabled ? targetLanguages || '-' : '-'}</strong>
        </div>
        <div className="summary-item">
          <span>内容类型</span>
          <strong>{translationEnabled ? translationContentTypeLabel : '-'}</strong>
        </div>
      </div>
      <div className="page-actions">
        <button onClick={onPrev}>上一步</button>
        <button disabled={finishing} onClick={onComplete}>
          {finishing ? '完成中…' : '完成初始化'}
        </button>
      </div>
    </div>
  )
}
