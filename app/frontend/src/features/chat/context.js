// What every transcript block needs to know about the session it belongs to.
import { createContext, useContext } from 'react'

/**
 * {sessionId, projectId, kind, workspace, active, turnStart}. workspace is the modeling agent's
 * "workspaces/<slug>"; active says the agent is working now; turnStart is when the last message was sent, in ms.
 */
export const ChatContext = createContext({ sessionId: null, projectId: null, kind: null, workspace: null, active: false, turnStart: null })

export function useChat() {
  return useContext(ChatContext)
}

/** The session's sub-agent runs (recursive calls), for nested transcripts. */
export const CallsContext = createContext([])

export function useCalls() {
  return useContext(CallsContext)
}
