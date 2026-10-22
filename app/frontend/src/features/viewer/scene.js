// What the viewer currently shows, published by ViewerPanel so other panels
// can ask "is this model loaded?" without reaching into Mol*.
import { create } from 'zustand'

export const useViewerScene = create(() => ({
  /** Layer snapshots from the controller: [{key, kind, label, status, visible, ...}] */
  layers: [],
}))

export function publishLayers(layers) {
  useViewerScene.setState({ layers })
}

/** True when a layer with this key has finished loading. */
export function useLayerReady(key) {
  return useViewerScene((state) => state.layers.some((layer) => layer.key === key && layer.status === 'ready'))
}
