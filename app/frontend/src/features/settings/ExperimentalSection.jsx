// Settings > Experimental: features that are still being tried out. Turned on,
// reconstruction sessions started from then on can search the web and read pages.
import { useApp } from '../../store/app'
import { Switch } from '../../components/ui'
import { SettingsGroup, SettingsRow, useSaveSettings } from './controls'

export function ExperimentalSection() {
  const enabled = useApp((state) => Boolean(state.setup?.experimental?.enabled))
  const save = useSaveSettings()
  return (
    <>
      <SettingsGroup>
        <SettingsRow
          title="Experimental features"
          description="Features that are still being tested. They can change or go away in a later version."
          htmlFor="settings-experimental"
        >
          <Switch
            id="settings-experimental"
            label="Experimental features"
            checked={enabled}
            onCheckedChange={(checked) => save({ experimental: { enabled: checked } }).catch(() => {})}
          />
        </SettingsRow>
      </SettingsGroup>
      <SettingsGroup title="What this turns on">
        <SettingsRow
          title="Web search and page reading"
          description="The reconstruction agent can search the web with DuckDuckGo, search papers in Europe PMC, and read public web pages, for example the CryoSPARC guide or a methods section. Neither service needs an account."
        >
          <span className="text-sm text-muted">{enabled ? 'On' : 'Off'}</span>
        </SettingsRow>
      </SettingsGroup>
      <p className="mt-4 text-sm text-muted">
        This applies to reconstruction sessions you start after you change it. A session keeps the setting it started with, also when you resume it, and its run report says
        so. Search queries go to DuckDuckGo or Europe PMC, and pages the agent reads see this computer&apos;s address. The agent cannot read addresses on this computer or the
        local network, and it treats web pages as reference material, not as instructions.
      </p>
    </>
  )
}
