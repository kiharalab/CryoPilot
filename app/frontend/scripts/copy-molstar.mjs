// Copies the prebuilt Mol* viewer bundle into public/ so the app is self-contained.
import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const source = join(root, 'node_modules', 'molstar', 'build', 'viewer')
const target = join(root, 'public', 'molstar')
mkdirSync(target, { recursive: true })
for (const name of ['molstar.js', 'molstar.css']) {
  const from = join(source, name)
  if (!existsSync(from)) {
    console.error(`Mol* bundle missing: ${from}. Run npm install first.`)
    process.exit(1)
  }
  copyFileSync(from, join(target, name))
}
console.log('Mol* viewer bundle copied to public/molstar')
