import { Link } from 'react-router-dom'

import { AppConfig, sourceLanguageOptions } from '../../api'
import { StepCard } from '../SettingsWidgets'
import { AsrStepProps } from './types'

const computeTypeOptions = [
  { value: 'auto', label: 'Auto' },
  { value: 'float32', label: 'FP32 / float32' },
  { value: 'float16', label: 'FP16 / float16' },
  { value: 'bfloat16', label: 'BF16 / bfloat16' },
  { value: 'int8', label: 'INT8' },
  { value: 'int8_float16', label: 'INT8 + FP16' },
  { value: 'int8_float32', label: 'INT8 + FP32' },
  { value: 'int8_bfloat16', label: 'INT8 + BF16' },
  { value: 'int16', label: 'INT16' },
]

const torchDtypeOptions = [
  { value: 'auto', label: 'Auto' },
  { value: 'float32', label: 'FP32 / float32' },
  { value: 'float16', label: 'FP16 / float16' },
  { value: 'bfloat16', label: 'BF16 / bfloat16' },
]

export function AsrStep({
  config,
  setField,
  expanded,
  onToggle,
  installedAsrModels,
  selectedAsrModel,
  currentProvider,
  sourceLanguageLabel,
  onSelectAsrModel,
}: AsrStepProps) {
  return (
    <StepCard
      index="02"
      title="语音识别"
      description="选择识别模型、源语言与常用识别参数。"
      statusLabel={selectedAsrModel?.status === 'installed' ? '已就绪' : '待安装'}
      tone={selectedAsrModel?.status === 'installed' ? 'success' : 'warning'}
      pills={[
        selectedAsrModel?.display_name || config.whisper.model_name,
        `语言 ${sourceLanguageLabel}`,
        currentProvider,
      ]}
      expanded={expanded}
      onToggle={onToggle}
      basicContent={
        <div className="field-grid">
          <label>
            <span>识别模型</span>
            <select
              value={config.whisper.model_name}
              onChange={(event) => onSelectAsrModel(event.target.value)}
              disabled={installedAsrModels.length === 0}
            >
              {installedAsrModels.length === 0 ? (
                <option value={config.whisper.model_name}>暂无已安装模型</option>
              ) : null}
              {installedAsrModels.map((model) => (
                <option key={model.name} value={model.name}>
                  {model.display_name}
                </option>
              ))}
            </select>
            <span className="muted">
              {selectedAsrModel?.description || '可在模型管理页下载后在这里直接切换。'}
            </span>
          </label>
          <label>
            <span>视频源语言</span>
            <select
              value={config.subtitle.source_language}
              onChange={(event) =>
                setField('subtitle', 'source_language', event.target.value as AppConfig['subtitle']['source_language'])
              }
            >
              {sourceLanguageOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      }
      advancedContent={
        <section className="advanced-section">
          <div className="field-grid">
            <label>
              <span>Beam Size</span>
              <input
                type="number"
                value={config.whisper.beam_size}
                onChange={(event) => setField('whisper', 'beam_size', Number(event.target.value))}
              />
            </label>
            <label className="switch-row">
              <span>VAD 过滤</span>
              <input
                type="checkbox"
                checked={config.whisper.vad_filter}
                onChange={(event) => setField('whisper', 'vad_filter', event.target.checked)}
              />
            </label>
            <label>
              <span>VAD 阈值</span>
              <input
                type="number"
                step="0.1"
                value={config.whisper.vad_threshold}
                onChange={(event) => setField('whisper', 'vad_threshold', Number(event.target.value))}
              />
            </label>
            <label>
              <span>音频格式</span>
              <select
                value={config.whisper.audio_format}
                onChange={(event) => setField('whisper', 'audio_format', event.target.value)}
              >
                <option value="wav">wav</option>
                <option value="mp3">mp3</option>
              </select>
            </label>
            <label>
              <span>采样率</span>
              <input
                type="number"
                value={config.whisper.sample_rate}
                onChange={(event) => setField('whisper', 'sample_rate', Number(event.target.value))}
              />
            </label>
            <label>
              <span>设备</span>
              <input type="text" value={config.whisper.device} readOnly disabled />
            </label>
            {currentProvider === 'whisperx' && (
              <>
                <label>
                  <span>WhisperX 对齐扩展时长</span>
                  <input
                    type="number"
                    value={config.whisper.advanced.whisperx_align_extend}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        whisperx_align_extend: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  <span>WhisperX Compute Type</span>
                  <select
                    value={config.whisper.advanced.whisperx_compute_type ?? 'auto'}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        whisperx_compute_type: event.target.value,
                      })
                    }
                  >
                    {computeTypeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {currentProvider === 'faster-whisper' && (
              <>
                <label>
                  <span>Faster-Whisper Compute Type</span>
                  <select
                    value={config.whisper.advanced.faster_whisper_compute_type ?? 'auto'}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        faster_whisper_compute_type: event.target.value,
                      })
                    }
                  >
                    {computeTypeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="switch-row">
                  <span>Faster-Whisper 词级时间戳</span>
                  <input
                    type="checkbox"
                    checked={config.whisper.advanced.faster_whisper_word_timestamps}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        faster_whisper_word_timestamps: event.target.checked,
                      })
                    }
                  />
                </label>
              </>
            )}
            {currentProvider === 'anime-whisper' && (
              <>
                <label>
                  <span>Anime-Whisper DType</span>
                  <select
                    value={config.whisper.advanced.anime_whisper_dtype ?? 'auto'}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        anime_whisper_dtype: event.target.value,
                      })
                    }
                  >
                    {torchDtypeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="switch-row">
                  <span>Anime-Whisper 对话增强</span>
                  <input
                    type="checkbox"
                    checked={config.whisper.advanced.anime_whisper_enhance_dialogue}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        anime_whisper_enhance_dialogue: event.target.checked,
                      })
                    }
                  />
                </label>
              </>
            )}
            {currentProvider === 'qwen' && (
              <>
                <label>
                  <span>Qwen DType</span>
                  <select
                    value={config.whisper.advanced.qwen_dtype ?? 'auto'}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        qwen_dtype: event.target.value,
                      })
                    }
                  >
                    {torchDtypeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Qwen Temperature</span>
                  <input
                    type="number"
                    step="0.1"
                    value={config.whisper.advanced.qwen_temperature}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        qwen_temperature: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  <span>Qwen 最大推理批次大小</span>
                  <input
                    type="number"
                    step="1"
                    min="1"
                    value={config.whisper.advanced.qwen_max_inference_batch_size}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        qwen_max_inference_batch_size: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  <span>Qwen 最大新 Token 数</span>
                  <input
                    type="number"
                    step="1"
                    min="1"
                    value={config.whisper.advanced.qwen_max_new_tokens}
                    onChange={(event) =>
                      setField('whisper', 'advanced', {
                        ...config.whisper.advanced,
                        qwen_max_new_tokens: Number(event.target.value),
                      })
                    }
                  />
                </label>
              </>
            )}
            <div className="field-block pipeline-wide">
              <span className="field-label">模型管理</span>
              <div className="model-summary">
                <span className={`status-chip ${selectedAsrModel?.status || 'not_installed'}`}>
                  {selectedAsrModel?.status || 'not_installed'}
                </span>
                <Link to="/models">前往模型管理页下载或删除模型</Link>
              </div>
            </div>
          </div>
        </section>
      }
    />
  )
}
