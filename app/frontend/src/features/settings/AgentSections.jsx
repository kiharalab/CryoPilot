// Settings > Reconstruction and Settings > Modeling: the processing guide,
// each agent's default model and reasoning effort, the modeling agent's turn
// limit, and its toolchain.
import { useEffect, useState } from 'react'
import { useApp } from '../../store/app'
import { Input } from '../../components/ui'
import { RECONSTRUCTION_EFFORTS } from '../new/AgentChips'
import { supportsEffort } from '../setup/services'
import { EffortPicker, ModelInput, SettingsGroup, SettingsRow, useSaveSettings } from './controls'
import { GuidePicker } from './GuidePicker'
import { ToolchainStatus } from './ToolchainStatus'

const MAX_TURNS_LIMIT = 5000

/** Saves one key of `defaults` and keeps a per-key error for inline display. */
function useDefaultsSaver() {
  const save = useSaveSettings()
  const [errors, setErrors] = useState({})
  const saveDefault = async (key, value) => {
    setErrors((current) => ({ ...current, [key]: '' }))
    try {
      await save({ defaults: { [key]: value } })
      return true
    } catch (err) {
      setErrors((current) => ({ ...current, [key]: err.message }))
      return false
    }
  }
  // A default model that does not accept the default effort moves it to Low.
  const saveModel = async (modelKey, effortKey, model, effort) => {
    if ((await saveDefault(modelKey, model)) && !supportsEffort(model, effort)) await saveDefault(effortKey, 'low')
  }
  return { errors, setErrors, saveDefault, saveModel }
}

export function ReconstructionSection() {
  const guide = useApp((state) => state.setup?.processing_guide)
  const defaults = useApp((state) => state.setup?.defaults) || {}
  const save = useSaveSettings()
  const [guideSaving, setGuideSaving] = useState(false)
  const { errors, saveDefault, saveModel } = useDefaultsSaver()

  const chooseGuide = async (id) => {
    setGuideSaving(true)
    try {
      await save({ processing_guide: id }, { errorTitle: 'Could not change the processing guide' })
    } catch {
      // toasted by useSaveSettings
    } finally {
      setGuideSaving(false)
    }
  }

  return (
    <>
      <SettingsGroup
        title="Processing guide"
        description="The reconstruction agent follows this guide as it plans and checks each processing stage. A change applies to new sessions; each session keeps the guide it started with."
      >
        <GuidePicker value={guide} onChange={chooseGuide} disabled={guideSaving} />
      </SettingsGroup>

      <SettingsGroup title="Defaults" description="New reconstruction sessions start with these. You can change both when you start a session.">
        <SettingsRow
          title="Model"
          description="Any OpenAI model, or a Claude model with an Anthropic API key."
          htmlFor="settings-reconstruction-model"
          error={errors.reconstruction_model}
        >
          <ModelInput
            id="settings-reconstruction-model"
            className="w-56"
            value={defaults.reconstruction_model}
            invalid={Boolean(errors.reconstruction_model)}
            onCommit={(model) => saveModel('reconstruction_model', 'reconstruction_effort', model, defaults.reconstruction_effort)}
          />
        </SettingsRow>
        <SettingsRow title="Reasoning effort" description="Higher effort plans each step more carefully and takes longer." error={errors.reconstruction_effort}>
          <EffortPicker
            value={defaults.reconstruction_effort}
            options={RECONSTRUCTION_EFFORTS}
            model={defaults.reconstruction_model}
            onChange={(effort) => saveDefault('reconstruction_effort', effort)}
          />
        </SettingsRow>
      </SettingsGroup>
    </>
  )
}

function MaxTurnsInput({ id, value, onCommit, invalid }) {
  const [text, setText] = useState(String(value ?? ''))
  useEffect(() => setText(String(value ?? '')), [value])

  const commit = () => {
    const trimmed = text.trim()
    if (trimmed === String(value ?? '')) return
    onCommit(trimmed)
  }

  return (
    <Input
      id={id}
      type="number"
      inputMode="numeric"
      min={1}
      max={MAX_TURNS_LIMIT}
      step={1}
      value={text}
      invalid={invalid}
      onChange={(event) => setText(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          commit()
        }
        if (event.key === 'Escape') setText(String(value ?? ''))
      }}
      className="w-28 text-right tabular-nums"
    />
  )
}

export function ModelingSection() {
  const defaults = useApp((state) => state.setup?.defaults) || {}
  const { errors, setErrors, saveDefault, saveModel } = useDefaultsSaver()

  const saveTurns = (text) => {
    const turns = Number(text)
    if (!Number.isInteger(turns) || turns < 1 || turns > MAX_TURNS_LIMIT) {
      setErrors((current) => ({ ...current, modeling_max_turns: `Enter a whole number from 1 to ${MAX_TURNS_LIMIT}.` }))
      return
    }
    saveDefault('modeling_max_turns', turns)
  }

  return (
    <>
      <SettingsGroup title="Defaults" description="New modeling sessions start with these. You can change the model and effort when you start a session.">
        <SettingsRow title="Model" description="Any OpenAI model, or a Claude model with an Anthropic API key." htmlFor="settings-modeling-model" error={errors.modeling_model}>
          <ModelInput
            id="settings-modeling-model"
            className="w-56"
            value={defaults.modeling_model}
            invalid={Boolean(errors.modeling_model)}
            onCommit={(model) => saveModel('modeling_model', 'modeling_effort', model, defaults.modeling_effort)}
          />
        </SettingsRow>
        <SettingsRow title="Reasoning effort" description="Higher effort reasons more about each fit and edit and takes longer." error={errors.modeling_effort}>
          <EffortPicker
            value={defaults.modeling_effort}
            model={defaults.modeling_model}
            onChange={(effort) => saveDefault('modeling_effort', effort)}
          />
        </SettingsRow>
        <SettingsRow
          title="Maximum turns"
          description="The most model turns the agent takes for one message before it stops and reports back."
          htmlFor="settings-max-turns"
          error={errors.modeling_max_turns}
        >
          <MaxTurnsInput
            id="settings-max-turns"
            value={defaults.modeling_max_turns}
            invalid={Boolean(errors.modeling_max_turns)}
            onCommit={saveTurns}
          />
        </SettingsRow>
      </SettingsGroup>

      <SettingsGroup
        title="Toolchain"
        description="Programs the modeling agent runs on this computer. Without an optional program, only the steps that need it are unavailable."
      >
        <ToolchainStatus />
      </SettingsGroup>
    </>
  )
}
