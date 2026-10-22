// highlight.js with only the languages CryoPilot shows: agent Python, shell
// commands, JSON tool arguments, and a few data formats in the file viewer.
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import ini from 'highlight.js/lib/languages/ini'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

hljs.registerLanguage('python', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('ini', ini)
hljs.registerLanguage('xml', xml)

const EXTENSIONS = {
  py: 'python',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  json: 'json',
  md: 'markdown',
  yml: 'yaml',
  yaml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  xml: 'xml',
  html: 'xml',
}

/** Language name for a file name, or null when it should stay plain text. */
export function languageForFile(name = '') {
  const extension = name.toLowerCase().split('.').pop()
  return EXTENSIONS[extension] || null
}

/** HTML for highlighted code; plain escaped text when the language is unknown or the code is huge. */
export function highlight(code, language) {
  const text = code || ''
  if (!language || !hljs.getLanguage(language) || text.length > 200_000) return escapeHtml(text)
  try {
    return hljs.highlight(text, { language, ignoreIllegals: true }).value
  } catch {
    return escapeHtml(text)
  }
}

export function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
