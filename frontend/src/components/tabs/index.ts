/*
 * Tabs for a page (TABS1, D319): chat's conversations first, then workflows.
 * Synced to cloud through UI_PIN with the rest of the kit; only where tabs are
 * saved differs between the products.
 */

export * from "./model";
export { BadgeMark, TabStrip, TabTile, type StripTab } from "./TabStrip";
export { NewTabMenu, type PickableAgent } from "./NewTabMenu";
export { TabSwitcher } from "./TabSwitcher";
export { SideRail, type RailPanel } from "./SideRail";
export { useTabShortcuts } from "./useTabShortcuts";
export { useTabStatus } from "./useTabStatus";
export { MAX_PINS, restorePins, useChatTabs, type Pin, type SavedChatTabs } from "./useChatTabs";
export { useWorkflowTabs, type AskAtCap, type FrontReport, type KeptTab } from "./useWorkflowTabs";
export type { EditorTabs } from "./editorTabs";
export {
  assistantChatHref, cleanDraft, draftHome, MAX_DRAFT, NEW_ASSISTANT_CHAT, readAssistantDraft, SUGGESTED_LABEL,
  withoutAssistantDraft, type DraftHome,
} from "./assistantDraft";
